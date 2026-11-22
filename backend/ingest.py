"""Small, resumable metadata sync from the live MGnify API v2 (no sequence files)."""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import duckdb
import polars as pl

BASE = "https://www.ebi.ac.uk/metagenomics/api/v2/"
ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "mgnify"
DB = Path(os.getenv("ATLAS_DB", ROOT / "data" / "atlas.duckdb"))
ENV_WORDS = ("environmental", "marine", "freshwater", "soil", "sediment", "ocean", "lake", "river", "wetland", "polar", "ice", "hot spring", "desert", "forest", "air")
EXCLUDE_WORDS = ("human gut", "stool", "feces", "faeces", "clinical", "disease", "oral microbiome", "skin microbiome")

def get(url: str, retries: int = 4):
    for attempt in range(retries):
        try:
            req = Request(url, headers={"Accept":"application/json", "User-Agent":"MicrobialAtlas/0.1 (open source research client)"})
            with urlopen(req, timeout=45) as res:
                payload = res.read()
            digest = hashlib.sha256(payload).hexdigest()
            cache_dir = RAW / "responses"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path = cache_dir / f"{digest}.json"
            if not cache_path.exists():
                cache_path.write_bytes(payload)
            metadata_path=cache_dir / f"{digest}.meta.json"
            if not metadata_path.exists():
                metadata_path.write_text(json.dumps({"source_url":url,"retrieved_at":datetime.now(timezone.utc).isoformat(),"sha256":digest},indent=2))
            return json.loads(payload), digest
        except Exception:
            if attempt == retries - 1: raise
            time.sleep(min(2 ** attempt, 12))

def attrs(item):
    return item.get("attributes", item) if isinstance(item, dict) else {}

def value(obj, *keys):
    for key in keys:
        v = obj.get(key)
        if v not in (None, "", "unknown", "not provided"): return v
    return None

def biome_label(obj):
    raw=value(obj,"biome","biome_name","biome_lineage")
    if isinstance(raw,dict):
        return raw.get("biome_name") or raw.get("name") or raw.get("lineage")
    return raw

def biome_path(obj):
    raw=value(obj,"biome","biome_name","biome_lineage")
    if isinstance(raw,dict): return raw.get("lineage")
    return obj.get("biome_lineage")

def items(payload):
    data = payload.get("data", payload.get("items", [])) if isinstance(payload, dict) else []
    if isinstance(data, dict): data = [data]
    return data if isinstance(data, list) else []

def record_accession(item):
    a = attrs(item)
    return item.get("id") or a.get("accession")

def pages(url: str):
    page = 1
    while url:
        parts=urlsplit(url); params=dict(parse_qsl(parts.query)); params.update({"page":str(page),"page_size":"100"})
        page_url=urlunsplit((parts.scheme,parts.netloc,parts.path,urlencode(params),parts.fragment))
        payload, _ = get(page_url)
        batch = items(payload)
        yield from batch
        count = payload.get("count") if isinstance(payload, dict) else None
        if not batch or (count is not None and page * 100 >= int(count)) or len(batch) < 100:
            break
        page += 1

def ensure_analysis_schema(con):
    con.execute("CREATE TABLE IF NOT EXISTS atlas_state (key VARCHAR PRIMARY KEY, updated_at TIMESTAMP)")
    con.execute("""CREATE TABLE IF NOT EXISTS runs (
        accession VARCHAR PRIMARY KEY, sample_accession VARCHAR, study_accession VARCHAR,
        experiment_type VARCHAR, instrument_model VARCHAR, instrument_platform VARCHAR,
        source_url VARCHAR, source_json JSON)""")
    con.execute("""CREATE TABLE IF NOT EXISTS analyses (
        accession VARCHAR PRIMARY KEY, run_accession VARCHAR, sample_accession VARCHAR,
        study_accession VARCHAR, experiment_type VARCHAR, pipeline_version VARCHAR,
        pipeline_family VARCHAR, result_dir VARCHAR, source_url VARCHAR, source_json JSON,
        detail_checked_at TIMESTAMP)""")
    con.execute("""CREATE TABLE IF NOT EXISTS analysis_downloads (
        analysis_accession VARCHAR, file_type VARCHAR, download_type VARCHAR,
        download_group VARCHAR, alias VARCHAR, short_description VARCHAR,
        file_size_bytes BIGINT, url VARCHAR, index_files JSON,
        PRIMARY KEY (analysis_accession, url))""")
    con.execute("""CREATE TABLE IF NOT EXISTS analysis_sync (
        study_accession VARCHAR PRIMARY KEY, checked_at TIMESTAMP, analysis_count BIGINT,
        details_checked BIGINT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS taxonomy_observations (
        observation_key VARCHAR PRIMARY KEY, analysis_accession VARCHAR, sample_accession VARCHAR,
        run_accession VARCHAR, taxonomy_method VARCHAR, reference_database VARCHAR,
        taxonomy_id VARCHAR, scientific_name VARCHAR, taxonomic_rank VARCHAR,
        taxonomic_lineage VARCHAR, reported_value DOUBLE, value_semantics VARCHAR,
        source_url VARCHAR, source_checksum VARCHAR, parser_version VARCHAR)""")
    con.execute("""CREATE TABLE IF NOT EXISTS artifact_ingestion (
        source_url VARCHAR PRIMARY KEY, analysis_accession VARCHAR, file_type VARCHAR,
        source_checksum VARCHAR, observation_count BIGINT, parser_version VARCHAR,
        ingestion_status VARCHAR, ingested_at TIMESTAMP)""")
    con.execute("""CREATE TABLE IF NOT EXISTS functional_observations (
        observation_key VARCHAR PRIMARY KEY, analysis_accession VARCHAR, sample_accession VARCHAR,
        run_accession VARCHAR, annotation_method VARCHAR, annotation_database VARCHAR,
        function_id VARCHAR, function_name VARCHAR, functional_category VARCHAR,
        reported_value DOUBLE, value_semantics VARCHAR, details_json JSON,
        source_url VARCHAR, source_checksum VARCHAR, parser_version VARCHAR)""")

def parse_biom_taxonomy_tsv(content: str):
    """Parse the verified V6 BIOM taxonomy-summary TSV format, preserving its raw counts."""
    lines = [line for line in content.splitlines() if line.strip()]
    header_line = next((line for line in lines if line.startswith("# OTU ID\t")), None)
    if header_line is None:
        raise ValueError("Not a supported MGnify BIOM taxonomy-summary TSV")
    header = [field.strip().lstrip("# ") for field in header_line.split("\t")]
    if len(header) < 3 or header[0] != "OTU ID" or header[2] != "taxonomy":
        raise ValueError("Unexpected BIOM taxonomy-summary columns")
    database = header[1]
    code_names = {"d":"Domain","sk":"Superkingdom","k":"Kingdom","sg":"Supergroup","dv":"Division","sdv":"Subdivision","p":"Phylum","c":"Class","o":"Order","f":"Family","g":"Genus","s":"Species"}
    parsed = []
    reader = csv.reader((line for line in lines if not line.startswith("# Constructed")), delimiter="\t")
    for row in reader:
        if not row or row[0].lstrip("# ") != "OTU ID":
            continue
        # This is the file header; skip it after validating above.
        break
    else:
        raise ValueError("Missing BIOM taxonomy-summary header")
    for row in reader:
        if len(row) < 3:
            continue
        try: reported = float(row[1])
        except (ValueError, TypeError): continue
        if not (reported >= 0 and reported < float("inf")): continue
        lineage = row[2].strip()
        named = []
        for token in lineage.split(";"):
            if "__" not in token: continue
            prefix, name = token.split("__", 1)
            if name.strip(): named.append((code_names.get(prefix, prefix), name.strip()))
        rank, name = named[-1] if named else ("Unclassified", "Unclassified")
        taxid = row[3].strip() if len(row) > 3 and row[3].strip() else None
        parsed.append({"taxonomy_method":"MGnify V6 amplicon taxonomy summary", "reference_database":database, "taxonomy_id":taxid, "scientific_name":name, "taxonomic_rank":rank, "taxonomic_lineage":lineage, "reported_value":reported, "value_semantics":"BIOM taxonomy-summary count as reported; not normalized abundance", "otu_id":row[0].strip()})
    if not parsed: raise ValueError("No taxonomy rows found in BIOM taxonomy-summary TSV")
    return parsed

def parse_assembly_function_tsv(content: str, filename: str):
    """Parse explicitly supported MGnify V6 assembly summary tables by their real headers."""
    lower=filename.lower()
    if lower.endswith("_pfam_summary.tsv.gz"):
        kind,expected,id_col,name_col,value_col,category_col="Pfam","pfam","pfam","description","count",None
        semantics="Reported Pfam annotation count; predicted protein annotation, not expression or activity"
    elif lower.endswith("_ko_summary.tsv.gz"):
        kind,expected,id_col,name_col,value_col,category_col="KEGG Orthology","ko","ko","description","count",None
        semantics="Reported KEGG Orthology annotation count; predicted function, not expression or activity"
    elif lower.endswith("_go_summary.tsv.gz") or lower.endswith("_goslim_summary.tsv.gz"):
        kind="GO slim" if "goslim" in lower else "Gene Ontology"
        expected="go"; id_col="go"; name_col="term"; value_col="count"; category_col="category"
        semantics="Reported GO annotation count; predicted protein annotation, not expression or activity"
    elif lower.endswith("_interpro_summary.tsv.gz"):
        kind,expected,id_col,name_col,value_col,category_col="InterPro","interpro_accession","interpro_accession","description","count",None
        semantics="Reported InterPro signature count; predicted protein annotation, not expression or activity"
    elif lower.endswith("_kegg_modules_summary.tsv.gz"):
        kind,expected,id_col,name_col,value_col,category_col="KEGG module","module_accession","module_accession","pathway_name","completeness","pathway_class"
        semantics="Reported KEGG module completeness percentage; not measured metabolic activity"
    else:
        raise ValueError("Unsupported MGnify V6 assembly functional summary filename")
    reader=csv.DictReader(io.StringIO(content),delimiter="\t")
    if not reader.fieldnames or expected not in [h.strip() for h in reader.fieldnames]:
        raise ValueError("Functional summary header does not match its recognized format")
    parsed=[]
    for row in reader:
        try: number=float(row.get(value_col,""))
        except (ValueError,TypeError): continue
        if number<0 or number==float("inf") or number!=number: continue
        record_id=(row.get(id_col) or "").strip()
        if not record_id: continue
        parsed.append({"annotation_method":"MGnify V6 assembly annotation","annotation_database":kind,"function_id":record_id,"function_name":(row.get(name_col) or "").strip() or record_id,"functional_category":(row.get(category_col) or "").strip() if category_col else None,"reported_value":number,"value_semantics":semantics,"details":row})
    if not parsed: raise ValueError("No supported functional records found in summary TSV")
    return parsed

def sync_profiles(limit: int = 20):
    """Fetch only explicitly reported V6 amplicon BIOM taxonomy TSVs; no sequence files."""
    with duckdb.connect(str(DB)) as con:
        ensure_analysis_schema(con)
        pending = con.execute("""WITH candidates AS (
            SELECT d.analysis_accession,a.sample_accession,a.run_accession,d.file_type,d.alias,d.url,
              row_number() OVER (PARTITION BY d.alias ORDER BY d.url) AS method_order
            FROM analysis_downloads d JOIN analyses a ON a.accession=d.analysis_accession
            LEFT JOIN artifact_ingestion i ON i.source_url=d.url
            WHERE i.source_url IS NULL AND a.pipeline_version LIKE 'V6%' AND a.experiment_type='Amplicon'
              AND d.download_type='Taxonomic analysis' AND d.file_type='tsv'
              AND d.alias IN ('SILVA-SSU','SILVA-LSU','PR2','ITSoneDB')
              AND i.source_url IS NULL)
            SELECT analysis_accession,sample_accession,run_accession,file_type,alias,url FROM candidates
            ORDER BY method_order, CASE alias WHEN 'SILVA-SSU' THEN 0 WHEN 'PR2' THEN 1 WHEN 'SILVA-LSU' THEN 2 ELSE 3 END
            LIMIT ?""", [max(0,limit)]).fetchall()
    manifest_path=ROOT/"data"/"manifests"/"mgnify.jsonl"; manifest_path.parent.mkdir(parents=True,exist_ok=True)
    artifact_dir=RAW/"artifacts"; artifact_dir.mkdir(parents=True,exist_ok=True)
    imported=0
    for analysis,sample,run,file_type,alias,url in pending:
        try:
            req=Request(url,headers={"User-Agent":"MicrobialAtlas/0.1 (open source research client)","Accept":"text/tab-separated-values,text/plain,*/*"})
            with urlopen(req,timeout=60) as response:
                payload=response.read(50_000_001)
            if len(payload)>50_000_000: raise ValueError("Artifact exceeds the 50 MB parser limit")
            checksum=hashlib.sha256(payload).hexdigest()
            content=payload.decode("utf-8-sig")
            rows=parse_biom_taxonomy_tsv(content)
            frame=pl.DataFrame(rows)
            observation_rows=[]
            for row in frame.to_dicts():
                key=hashlib.sha256(f"{url}\0{row['otu_id']}\0{row['taxonomic_lineage']}".encode()).hexdigest()
                observation_rows.append((key,analysis,sample,run,row["taxonomy_method"],row["reference_database"],row["taxonomy_id"],row["scientific_name"],row["taxonomic_rank"],row["taxonomic_lineage"],row["reported_value"],row["value_semantics"],url,checksum,"0.2.0"))
            digest_path=artifact_dir/f"{checksum}.tsv"
            if not digest_path.exists(): digest_path.write_bytes(payload)
            with duckdb.connect(str(DB)) as con:
                con.executemany("""INSERT INTO taxonomy_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(observation_key) DO NOTHING""", observation_rows)
                con.execute("INSERT INTO artifact_ingestion VALUES (?,?,?,?,?,?,?,now()) ON CONFLICT(source_url) DO NOTHING",[url,analysis,file_type,checksum,len(rows),"0.2.0","parsed"])
            with manifest_path.open("a") as f:
                f.write(json.dumps({"source":"MGnify API v2 reported artifact","retrieved_at":datetime.now(timezone.utc).isoformat(),"source_record_accession":analysis,"source_url":url,"study_accession":None,"sample_accession":sample,"run_accession":run,"analysis_accession":analysis,"pipeline_name":"Amplicon","pipeline_version":"V6","reference_database":alias,"original_filename":url.rsplit("/",1)[-1],"checksum":checksum,"parser_version":"0.2.0","ingestion_status":"parsed_taxonomy_summary","observation_count":len(rows)})+"\n")
            imported+=1
        except Exception as exc:
            print(f"Could not parse reported taxonomy artifact {url}: {exc}")
    with duckdb.connect(str(DB)) as con:
        con.execute("CREATE TABLE IF NOT EXISTS atlas_state (key VARCHAR PRIMARY KEY, updated_at TIMESTAMP)")
        con.execute("INSERT INTO atlas_state VALUES ('last_profile_sync',now()) ON CONFLICT(key) DO UPDATE SET updated_at=now()")
    print(json.dumps({"taxonomy_artifacts_parsed":imported,"attempted":len(pending),"limit":limit,"artifact_type":"V6 amplicon BIOM taxonomy-summary TSV"},indent=2))

def sync_functions(limit: int = 20):
    """Fetch only listed V6 assembly summary TSVs with a verified supported header."""
    with duckdb.connect(str(DB)) as con:
        ensure_analysis_schema(con)
        pending=con.execute("""WITH candidates AS (
            SELECT d.analysis_accession,a.sample_accession,a.run_accession,d.alias,d.url,
              row_number() OVER (PARTITION BY regexp_extract(d.alias,'_(pfam_summary|ko_summary|go_summary|goslim_summary|interpro_summary|kegg_modules_summary)\\.tsv\\.gz$',1) ORDER BY d.url) AS file_order
            FROM analysis_downloads d JOIN analyses a ON a.accession=d.analysis_accession
            LEFT JOIN artifact_ingestion i ON i.source_url=d.url
            WHERE i.source_url IS NULL AND a.pipeline_version LIKE 'V6%' AND a.experiment_type='Assembly'
              AND d.download_type='Functional analysis' AND d.file_type='tsv'
              AND (regexp_matches(lower(d.alias),'_(pfam_summary|ko_summary|go_summary|goslim_summary|interpro_summary|kegg_modules_summary)\\.tsv\\.gz$')))
            SELECT analysis_accession,sample_accession,run_accession,alias,url FROM candidates
            ORDER BY file_order,alias LIMIT ?""",[max(0,limit)]).fetchall()
    artifact_dir=RAW/"artifacts"; artifact_dir.mkdir(parents=True,exist_ok=True)
    manifest_path=ROOT/"data"/"manifests"/"mgnify.jsonl"; manifest_path.parent.mkdir(parents=True,exist_ok=True)
    imported=0
    for analysis,sample,run,alias,url in pending:
        try:
            req=Request(url,headers={"User-Agent":"MicrobialAtlas/0.1 (open source research client)","Accept":"application/gzip,application/octet-stream,*/*"})
            with urlopen(req,timeout=60) as response: payload=response.read(50_000_001)
            if len(payload)>50_000_000: raise ValueError("Compressed artifact exceeds the 50 MB limit")
            checksum=hashlib.sha256(payload).hexdigest()
            with gzip.GzipFile(fileobj=io.BytesIO(payload)) as gz: raw=gz.read(50_000_001)
            if len(raw)>50_000_000: raise ValueError("Decompressed artifact exceeds the 50 MB parser limit")
            rows=parse_assembly_function_tsv(raw.decode("utf-8-sig"),alias)
            frame=pl.DataFrame(rows); values=[]
            for row in frame.to_dicts():
                details=json.dumps(row["details"],sort_keys=True)
                key=hashlib.sha256(f"{url}\0{row['function_id']}\0{details}".encode()).hexdigest()
                values.append((key,analysis,sample,run,row["annotation_method"],row["annotation_database"],row["function_id"],row["function_name"],row["functional_category"],row["reported_value"],row["value_semantics"],details,url,checksum,"0.3.0"))
            digest_path=artifact_dir/f"{checksum}.gz"
            if not digest_path.exists(): digest_path.write_bytes(payload)
            with duckdb.connect(str(DB)) as con:
                con.executemany("""INSERT INTO functional_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(observation_key) DO NOTHING""",values)
                con.execute("INSERT INTO artifact_ingestion VALUES (?,?,?,?,?,?,?,now()) ON CONFLICT(source_url) DO NOTHING",[url,analysis,"tsv.gz",checksum,len(rows),"0.3.0","parsed"])
            with manifest_path.open("a") as f:
                f.write(json.dumps({"source":"MGnify API v2 reported artifact","retrieved_at":datetime.now(timezone.utc).isoformat(),"source_record_accession":analysis,"source_url":url,"study_accession":None,"sample_accession":sample,"run_accession":run,"analysis_accession":analysis,"pipeline_name":"Assembly","pipeline_version":"V6","reference_database":rows[0]["annotation_database"],"original_filename":alias,"checksum":checksum,"parser_version":"0.3.0","ingestion_status":"parsed_function_summary","observation_count":len(rows)})+"\n")
            imported+=1
        except Exception as exc:
            print(f"Could not parse reported functional artifact {url}: {exc}")
    with duckdb.connect(str(DB)) as con:
        con.execute("INSERT INTO atlas_state VALUES ('last_function_sync',now()) ON CONFLICT(key) DO UPDATE SET updated_at=now()")
    print(json.dumps({"functional_artifacts_parsed":imported,"attempted":len(pending),"limit":limit,"formats":["Pfam","KEGG Orthology","Gene Ontology","GO slim","InterPro","KEGG module"]},indent=2))

def sync_analyses(limit: int = 100):
    """Discover sample-linked runs/analyses and bounded analysis download metadata."""
    if not DB.exists():
        raise RuntimeError("No sample database found; run the sample metadata sync first")
    with duckdb.connect(str(DB)) as con:
        ensure_analysis_schema(con)
        study_rows = con.execute("SELECT DISTINCT study_accession FROM samples WHERE study_accession IS NOT NULL ORDER BY 1").fetchall()
        sample_studies = {r[0] for r in study_rows}
        known_samples = {r[0] for r in con.execute("SELECT accession FROM samples").fetchall()}
    analyses_found = {}; runs_found = {}; checked_studies = 0
    for study in sorted(sample_studies):
        url = f"{BASE}studies/{study}/analyses/"
        batch_count = 0
        for item in pages(url):
            a = attrs(item); sample = a.get("sample") or {}; run = a.get("run") or {}
            sample_id = (sample.get("accession") or sample.get("id") or a.get("sample_accession")) if isinstance(sample,dict) else None
            run_id = (run.get("accession") or run.get("id") or a.get("run_accession")) if isinstance(run,dict) else a.get("run_accession")
            analysis_id = record_accession(item)
            if sample_id not in known_samples or not analysis_id: continue
            batch_count += 1
            analyses_found[analysis_id] = {"accession":analysis_id,"run_accession":run_id,"sample_accession":sample_id,"study_accession":study,"experiment_type":a.get("experiment_type"),"pipeline_version":a.get("pipeline_version"),"pipeline_family":a.get("pipeline_family") or a.get("pipeline_name"),"source_url":f"{BASE}analyses/{analysis_id}","source_json":json.dumps(item)}
            if run_id:
                runs_found[run_id] = {"accession":run_id,"sample_accession":sample_id,"study_accession":study,"experiment_type":a.get("experiment_type"),"instrument_model":run.get("instrument_model") if isinstance(run,dict) else None,"instrument_platform":run.get("instrument_platform") if isinstance(run,dict) else None,"source_url":f"{BASE}runs/{run_id}","source_json":json.dumps(run)}
        with duckdb.connect(str(DB)) as con:
            con.execute("INSERT INTO analysis_sync VALUES (?, now(), ?, 0) ON CONFLICT(study_accession) DO UPDATE SET checked_at=now(), analysis_count=excluded.analysis_count",[study,batch_count])
        checked_studies += 1
    detail_checked = 0
    # Fetch detail responses only for a bounded, resumable set. URLs come from the live API accession.
    with duckdb.connect(str(DB)) as con:
        for acc,row in runs_found.items():
            con.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(accession) DO UPDATE SET sample_accession=excluded.sample_accession, study_accession=excluded.study_accession, experiment_type=excluded.experiment_type, instrument_model=excluded.instrument_model, instrument_platform=excluded.instrument_platform, source_json=excluded.source_json", [row[k] for k in ("accession","sample_accession","study_accession","experiment_type","instrument_model","instrument_platform","source_url","source_json")])
        for acc,row in analyses_found.items():
            con.execute("INSERT INTO analyses (accession,run_accession,sample_accession,study_accession,experiment_type,pipeline_version,pipeline_family,source_url,source_json) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(accession) DO UPDATE SET run_accession=excluded.run_accession, sample_accession=excluded.sample_accession, study_accession=excluded.study_accession, pipeline_version=excluded.pipeline_version, source_json=excluded.source_json", [row[k] for k in ("accession","run_accession","sample_accession","study_accession","experiment_type","pipeline_version","pipeline_family","source_url","source_json")])
        pending = con.execute("SELECT accession, source_url FROM analyses WHERE detail_checked_at IS NULL ORDER BY CASE WHEN pipeline_version LIKE 'V6%' AND experiment_type='Assembly' THEN 0 WHEN pipeline_version LIKE 'V6%' THEN 1 ELSE 2 END, accession LIMIT ?", [max(0,limit)]).fetchall()
    for acc,detail_url in pending:
        try:
            payload,_=get(detail_url); d=attrs(payload.get("data",payload)); downloads=d.get("downloads") or []
            with duckdb.connect(str(DB)) as con:
                con.execute("UPDATE analyses SET result_dir=?, detail_checked_at=current_timestamp WHERE accession=?",[d.get("results_dir"),acc])
                for f in downloads:
                    f=f if isinstance(f,dict) else {}
                    if not f.get("url"): continue
                    con.execute("INSERT INTO analysis_downloads VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(analysis_accession,url) DO UPDATE SET file_type=excluded.file_type,download_type=excluded.download_type,download_group=excluded.download_group,alias=excluded.alias,short_description=excluded.short_description,file_size_bytes=excluded.file_size_bytes,index_files=excluded.index_files", [acc,f.get("file_type"),f.get("download_type"),f.get("download_group"),f.get("alias"),f.get("short_description"),f.get("file_size_bytes"),f["url"],json.dumps(f.get("index_files")) if f.get("index_files") is not None else None])
            detail_checked+=1
        except Exception as exc:
            print(f"Could not fetch analysis detail {acc}: {exc}")
    with duckdb.connect(str(DB)) as con:
        con.execute("UPDATE samples SET analysis_count=(SELECT count(*) FROM analyses WHERE analyses.sample_accession=samples.accession)")
        con.execute("UPDATE analysis_sync SET details_checked=(SELECT count(*) FROM analyses a WHERE a.study_accession=analysis_sync.study_accession AND a.detail_checked_at IS NOT NULL)")
        con.execute("INSERT INTO atlas_state VALUES ('last_analysis_sync',now()) ON CONFLICT(key) DO UPDATE SET updated_at=now()")
        sample_rows=con.execute("SELECT * FROM samples ORDER BY accession").fetchall(); sample_columns=[d[0] for d in con.description]
        manifest_path=ROOT/"data"/"manifests"/"mgnify.jsonl"
        manifested=set()
        if manifest_path.exists():
            for line in manifest_path.open():
                try:
                    record=json.loads(line); accession=record.get("source_record_accession")
                    if accession: manifested.add(accession)
                except (json.JSONDecodeError,AttributeError): pass
        with manifest_path.open("a") as f:
            for row in con.execute("SELECT accession,sample_accession,study_accession,experiment_type,pipeline_version,source_url,source_json FROM analyses ORDER BY accession").fetchall():
                accession,sample_id,study_id,experiment,pipeline_version,source_url,source_json=row
                if accession in manifested: continue
                digest=hashlib.sha256(str(source_json).encode()).hexdigest()
                f.write(json.dumps({"source":"MGnify API v2","retrieved_at":datetime.now(timezone.utc).isoformat(),"source_record_accession":accession,"source_url":source_url,"study_accession":study_id,"sample_accession":sample_id,"run_accession":None,"analysis_accession":accession,"pipeline_name":experiment,"pipeline_version":pipeline_version,"reference_database":None,"original_filename":None,"checksum":digest,"parser_version":"0.2.0","ingestion_status":"analysis_metadata_imported"})+"\n")
                manifested.add(accession)
            for row in con.execute("SELECT accession,sample_accession,study_accession,experiment_type,instrument_model,instrument_platform,source_url,source_json FROM runs ORDER BY accession").fetchall():
                accession,sample_id,study_id,experiment,model,platform,source_url,source_json=row
                if accession in manifested: continue
                digest=hashlib.sha256(str(source_json).encode()).hexdigest()
                f.write(json.dumps({"source":"MGnify API v2","retrieved_at":datetime.now(timezone.utc).isoformat(),"source_record_accession":accession,"source_url":source_url,"study_accession":study_id,"sample_accession":sample_id,"run_accession":accession,"analysis_accession":None,"experiment_type":experiment,"instrument_model":model,"instrument_platform":platform,"pipeline_name":None,"pipeline_version":None,"reference_database":None,"original_filename":None,"checksum":digest,"parser_version":"0.2.0","ingestion_status":"run_metadata_imported"})+"\n")
                manifested.add(accession)
    silver=ROOT/"data"/"silver"; silver.mkdir(parents=True,exist_ok=True)
    pl.DataFrame(sample_rows,schema=sample_columns,orient="row").write_parquet(silver/"samples.parquet")
    print(json.dumps({"studies_checked":checked_studies,"linked_analyses_discovered":len(analyses_found),"linked_runs_discovered":len(runs_found),"analysis_details_checked_this_run":detail_checked,"detail_limit":limit},indent=2))

def sync(limit: int, pipeline: str | None = None):
    RAW.mkdir(parents=True, exist_ok=True); DB.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = ROOT / "data" / "manifests" / "mgnify.jsonl"; manifest_path.parent.mkdir(parents=True, exist_ok=True)
    existing = set()
    if DB.exists():
        try:
            with duckdb.connect(str(DB), read_only=True) as con: existing = {r[0] for r in con.execute("SELECT accession FROM samples").fetchall()}
        except Exception: pass
    found: dict[str,dict] = {}; study_records: dict[str,dict] = {}; studied: set[str] = set()
    study_url = BASE + "studies/"
    if pipeline:
        # These are documented MGnify v2 study-list filters, validated from OpenAPI.
        study_url += "?" + urlencode({"has_analyses_from_pipeline": pipeline, "biome_lineage": "root:Environmental", "order": "-updated_at", "page_size": 100})
    for study in pages(study_url):
        accession = record_accession(study)
        if not accession: continue
        a = attrs(study); biome = biome_label(a)
        desc = " ".join(str(value(a,k) or "") for k in ("biome","biome_name","biome_lineage","environmental_material","environmental_feature" )).lower()
        if not any(w in desc for w in ENV_WORDS) or any(w in desc for w in EXCLUDE_WORDS): continue
        detail, digest = get(f"{BASE}studies/{accession}")
        d = attrs(detail.get("data",detail)); source_samples = d.get("samples_url") or d.get("samples")
        study_metadata=d.get("metadata") if isinstance(d.get("metadata"),dict) else {}
        study_biome=d.get("biome") if isinstance(d.get("biome"),dict) else {}
        study_records[accession]={"accession":accession,"title":value(d,"title","study_title") or value(study_metadata,"study_title","title"),"description":value(d,"description","study_description") or value(study_metadata,"study_description","description"),"biome":biome_label(d),"biome_lineage":study_biome.get("lineage"),"updated_at":value(d,"updated_at"),"source_url":f"{BASE}studies/{accession}","source_checksum":digest,"source_json":json.dumps(detail,sort_keys=True)}
        links = detail.get("links",{}) if isinstance(detail,dict) else {}
        if isinstance(source_samples,dict): source_samples=source_samples.get("href") or source_samples.get("url")
        if not source_samples: source_samples = links.get("samples") if isinstance(links,dict) else None
        if not source_samples: source_samples=f"studies/{accession}/samples?page_size=100"
        source_samples=urljoin(BASE,source_samples)
        studied.add(accession)
        for sample in pages(source_samples):
            sa=attrs(sample); sample_id=record_accession(sample)
            if not sample_id or sample_id in found or sample_id in existing: continue
            meta=sa.get("sample_metadata",sa.get("metadata",[])); md={}
            if isinstance(meta,list): md={str(x.get("key",x.get("name",""))).lower().replace(" ","_"):x.get("value") for x in meta if isinstance(x,dict)}
            elif isinstance(meta,dict): md={str(k).lower().replace(" ","_"):v for k,v in meta.items()}
            lat=value(sa,"latitude","lat") or value(md,"latitude","lat","geographic_location_(latitude)")
            lon=value(sa,"longitude","lon","lng") or value(md,"longitude","lon","lng","geographic_location_(longitude)")
            try: lat=float(lat); lon=float(lon); valid=(-90<=lat<=90 and -180<=lon<=180)
            except (ValueError,TypeError): lat=lon=None; valid=False
            if not valid: lat=lon=None
            sample_biome=biome_label(sa) or biome
            study_biome=a.get("biome") if isinstance(a.get("biome"),dict) else {}
            lineage=biome_path(sa) or study_biome.get("lineage")
            found[sample_id]={"accession":sample_id,"study_accession":accession,"biome":str(sample_biome) if sample_biome else None,"biome_lineage":lineage,"latitude":lat,"longitude":lon,"collection_date":value(sa,"collection_date","sample_collection_date") or value(md,"collection_date","collection_date_(yyyy-mm-dd)"),"country":value(sa,"country") or value(md,"country","country_name"),"environmental_material":value(sa,"environmental_material") or value(md,"environmental_material"),"analysis_count":0,"source_url":f"{BASE}samples/{sample_id}"}
            found[sample_id]["source_checksum"]=hashlib.sha256(json.dumps(sample,sort_keys=True).encode()).hexdigest()
            if len(found) >= limit: break
        if len(found)>=limit: break
    # Sample records are the only entities counted here; analysis availability is linked separately.
    rows=list(found.values())
    frame=pl.DataFrame(rows, schema_overrides={"accession":pl.String,"study_accession":pl.String,"biome":pl.String,"biome_lineage":pl.String,"latitude":pl.Float64,"longitude":pl.Float64,"collection_date":pl.String,"country":pl.String,"environmental_material":pl.String,"analysis_count":pl.Int64,"source_url":pl.String,"source_checksum":pl.String}) if rows else pl.DataFrame(schema={"accession":pl.String,"study_accession":pl.String,"biome":pl.String,"biome_lineage":pl.String,"latitude":pl.Float64,"longitude":pl.Float64,"collection_date":pl.String,"country":pl.String,"environmental_material":pl.String,"analysis_count":pl.Int64,"source_url":pl.String,"source_checksum":pl.String})
    # Keep existing records and replace the small table atomically within a single DuckDB write session.
    with duckdb.connect(str(DB)) as con:
        con.execute("CREATE TABLE IF NOT EXISTS samples (accession VARCHAR PRIMARY KEY, study_accession VARCHAR, biome VARCHAR, biome_lineage VARCHAR, latitude DOUBLE, longitude DOUBLE, collection_date VARCHAR, country VARCHAR, environmental_material VARCHAR, analysis_count BIGINT, source_url VARCHAR, source_checksum VARCHAR)")
        con.execute("CREATE TABLE IF NOT EXISTS studies (accession VARCHAR PRIMARY KEY,title VARCHAR,description VARCHAR,biome VARCHAR,biome_lineage VARCHAR,updated_at VARCHAR,source_url VARCHAR,source_checksum VARCHAR,source_json JSON)")
        con.execute("CREATE TABLE IF NOT EXISTS atlas_state (key VARCHAR PRIMARY KEY, updated_at TIMESTAMP)")
        current_columns=[column[0] for column in con.execute("DESCRIBE samples").fetchall()]
        if "source_checksum" not in current_columns: con.execute("ALTER TABLE samples ADD COLUMN source_checksum VARCHAR")
        if "biome_lineage" not in current_columns: con.execute("ALTER TABLE samples ADD COLUMN biome_lineage VARCHAR")
        for study_row in study_records.values():
            con.execute("""INSERT INTO studies VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(accession) DO UPDATE SET
              title=coalesce(excluded.title,studies.title),description=coalesce(excluded.description,studies.description),
              biome=coalesce(excluded.biome,studies.biome),biome_lineage=coalesce(excluded.biome_lineage,studies.biome_lineage),
              updated_at=coalesce(excluded.updated_at,studies.updated_at),source_checksum=excluded.source_checksum,source_json=excluded.source_json""", [study_row[k] for k in ("accession","title","description","biome","biome_lineage","updated_at","source_url","source_checksum","source_json")])
        if rows:
            values=[tuple(row.get(column) for column in frame.columns) for row in frame.to_dicts()]
            placeholders=", ".join("?" for _ in frame.columns)
            con.executemany(f"INSERT INTO samples ({', '.join(frame.columns)}) VALUES ({placeholders}) ON CONFLICT(accession) DO NOTHING",values)
        stored=con.execute("SELECT * FROM samples ORDER BY accession").fetchall()
        columns=[d[0] for d in con.description]
        con.execute("INSERT INTO atlas_state VALUES ('last_sample_sync',now()) ON CONFLICT(key) DO UPDATE SET updated_at=now()")
    # Persist a stable analytical snapshot alongside DuckDB for reproducible downstream work.
    silver=ROOT / "data" / "silver"; silver.mkdir(parents=True,exist_ok=True)
    pl.DataFrame(stored,schema=columns,orient="row").write_parquet(silver / "samples.parquet")
    now=datetime.now(timezone.utc).isoformat()
    manifested_studies=set()
    if manifest_path.exists():
        for line in manifest_path.open():
            try:
                rec=json.loads(line)
                if rec.get("ingestion_status")=="study_metadata_imported": manifested_studies.add(rec.get("source_record_accession"))
            except (json.JSONDecodeError,AttributeError): pass
    with manifest_path.open("a") as f:
        for study_row in study_records.values():
            if study_row["accession"] in manifested_studies: continue
            f.write(json.dumps({"source":"MGnify API v2","retrieved_at":now,"source_record_accession":study_row["accession"],"source_url":study_row["source_url"],"study_accession":study_row["accession"],"sample_accession":None,"run_accession":None,"analysis_accession":None,"pipeline_name":None,"pipeline_version":None,"reference_database":None,"original_filename":None,"checksum":study_row["source_checksum"],"parser_version":"0.2.0","ingestion_status":"study_metadata_imported"})+"\n")
            manifested_studies.add(study_row["accession"])
        for row in rows:
            f.write(json.dumps({"source":"MGnify API v2","retrieved_at":now,"source_record_accession":row["accession"],"source_url":row["source_url"],"study_accession":row["study_accession"],"sample_accession":row["accession"],"run_accession":None,"analysis_accession":None,"pipeline_name":None,"pipeline_version":None,"reference_database":None,"original_filename":None,"checksum":row["source_checksum"],"parser_version":"0.1.0","ingestion_status":"metadata_imported"})+"\n")
    print(json.dumps({"source":"MGnify API v2","new_samples":len(rows),"previous_samples":len(existing),"candidate_studies":len(studied),"limit":limit,"georeferenced":sum(r["latitude"] is not None for r in rows),"database":str(DB)},indent=2))

if __name__ == "__main__":
    parser=argparse.ArgumentParser(); parser.add_argument("--limit",type=int,default=int(os.getenv("ATLAS_SAMPLE_LIMIT","2000"))); parser.add_argument("--pipeline",choices=("V1","V2","V3","V4","V4.1","V5","V6","V6.1","V6.2"),help="discover only environmentally classified studies with analyses from this MGnify pipeline"); parser.add_argument("--analyses",action="store_true",help="also sync sample-linked runs, analyses, and analysis download metadata"); parser.add_argument("--analysis-only",action="store_true",help="skip sample discovery and update runs/analyses for the already-imported samples"); parser.add_argument("--analysis-detail-limit",type=int,default=100); parser.add_argument("--profiles",action="store_true",help="download and parse reported V6 amplicon BIOM taxonomy TSV files"); parser.add_argument("--profile-limit",type=int,default=20); parser.add_argument("--functions",action="store_true",help="download and parse supported V6 assembly function-summary TSV files"); parser.add_argument("--function-limit",type=int,default=20); args=parser.parse_args(); sync(max(1,args.limit),args.pipeline) if not args.analysis_only else None; sync_analyses(args.analysis_detail_limit) if args.analyses or args.analysis_only else None; sync_profiles(args.profile_limit) if args.profiles else None; sync_functions(args.function_limit) if args.functions else None
