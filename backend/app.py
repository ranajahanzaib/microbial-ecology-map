from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import duckdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

ROOT = Path(__file__).resolve().parents[1]
DB = Path(os.getenv("ATLAS_DB", ROOT / "data" / "atlas.duckdb"))
app = FastAPI(title="Microbial Atlas API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?", allow_methods=["GET"], allow_headers=["*"])

def query(sql: str, args: list[Any] | None = None) -> list[dict[str, Any]]:
    if not DB.exists():
        return []
    try:
        with duckdb.connect(str(DB), read_only=True) as con:
            result = con.execute(sql, args or [])
            columns = [column[0] for column in result.description]
            return [dict(zip(columns, row)) for row in result.fetchall()]
    except duckdb.Error as exc:
        if "does not exist" in str(exc).lower():
            return []
        raise

@app.get("/api/v1/health")
def health():
    return {"status": "ok", "database_available": DB.exists(), "source": "MGnify API v2"}

def table_exists(name: str) -> bool:
    rows = query("SELECT count(*) AS n FROM information_schema.tables WHERE table_name = ?", [name])
    return bool(rows and rows[0]["n"])

@app.get("/api/v1/overview")
def overview():
    rows = query("SELECT count(*) AS unique_samples, count(DISTINCT study_accession) AS studies, count(*) FILTER (WHERE latitude IS NOT NULL AND longitude IS NOT NULL) AS georeferenced FROM samples")
    result = rows[0] if rows else {"unique_samples": 0, "studies": 0, "georeferenced": 0}
    has_analyses = table_exists("analyses")
    if has_analyses:
        counts = query("SELECT count(DISTINCT sample_accession) AS with_analyses, count(*) AS analysis_records FROM analyses")
        result.update(counts[0] if counts else {"with_analyses": 0, "analysis_records": 0})
    else:
        result.update({"with_analyses": 0, "analysis_records": 0})
    result["taxonomy_observations"] = query("SELECT count(*) AS n FROM taxonomy_observations")[0]["n"] if table_exists("taxonomy_observations") else 0
    result["taxonomy_profiles"] = query("SELECT count(DISTINCT analysis_accession || ':' || reference_database) AS n FROM taxonomy_observations")[0]["n"] if table_exists("taxonomy_observations") else 0
    result["functional_observations"] = query("SELECT count(*) AS n FROM functional_observations")[0]["n"] if table_exists("functional_observations") else 0
    result["functional_profiles"] = query("SELECT count(DISTINCT analysis_accession || ':' || annotation_database) AS n FROM functional_observations")[0]["n"] if table_exists("functional_observations") else 0
    sync_rows = query("SELECT count(*) AS checked_studies, max(checked_at) AS analyses_checked_at FROM analysis_sync") if table_exists("analysis_sync") else []
    result.update(sync_rows[0] if sync_rows else {"checked_studies": 0, "analyses_checked_at": None})
    state_rows=query("SELECT key,updated_at FROM atlas_state") if table_exists("atlas_state") else []
    state={row["key"]:row["updated_at"] for row in state_rows}
    result["last_sample_sync"] = state.get("last_sample_sync")
    result["last_analysis_sync"] = state.get("last_analysis_sync")
    result["last_profile_sync"] = state.get("last_profile_sync")
    result["last_function_sync"] = state.get("last_function_sync")
    result["analysis_discovery_complete"] = bool(result["checked_studies"] >= result["studies"] and result["studies"] > 0)
    result["source"] = "MGnify API v2"
    return result

@app.get("/api/v1/samples")
def samples(limit: int = Query(200, ge=1, le=2000), offset: int = Query(0, ge=0), biome: str | None = None, bbox: str | None = None):
    where = ["latitude IS NOT NULL", "longitude IS NOT NULL"]
    args: list[Any] = []
    if biome:
        where.append("lower(coalesce(biome, '')) LIKE ?")
        args.append(f"%{biome.lower()}%")
    if bbox:
        try:
            west, south, east, north = map(float, bbox.split(","))
            if not (-180 <= west <= 180 and -180 <= east <= 180 and -90 <= south <= 90 and -90 <= north <= 90): raise ValueError
        except ValueError as exc:
            raise HTTPException(422, "bbox must be west,south,east,north in geographic degrees") from exc
        where.append("longitude BETWEEN ? AND ?" if west <= east else "(longitude >= ? OR longitude <= ?)")
        where.append("latitude BETWEEN ? AND ?")
        args.extend([west, east, south, north])
    total = query(f"SELECT count(*) AS n FROM samples WHERE {' AND '.join(where)}", args)
    scoped_where=[term.replace("coalesce(biome", "coalesce(s.biome").replace("latitude", "s.latitude").replace("longitude", "s.longitude") for term in where]
    items = query(f"SELECT s.accession, s.study_accession, coalesce(s.biome,st.biome) AS biome, coalesce(s.biome_lineage,st.biome_lineage) AS biome_lineage, s.latitude, s.longitude, s.collection_date, s.country, s.environmental_material, s.analysis_count, st.title AS study_title FROM samples s LEFT JOIN studies st ON st.accession=s.study_accession WHERE {' AND '.join(scoped_where)} ORDER BY s.accession LIMIT ? OFFSET ?", args + [limit, offset]) if table_exists("studies") else query(f"SELECT accession, study_accession, biome, biome_lineage, latitude, longitude, collection_date, country, environmental_material, analysis_count, NULL AS study_title FROM samples WHERE {' AND '.join(where)} ORDER BY accession LIMIT ? OFFSET ?", args + [limit, offset])
    return {"total": total[0]["n"] if total else 0, "limit": limit, "offset": offset, "items": items}

@app.get("/api/v1/search/samples")
def search_samples(q: str = Query(min_length=2,max_length=120), limit: int = Query(20,ge=1,le=100)):
    term=f"%{q.strip()}%"
    if table_exists("studies"):
        sql="""SELECT s.accession,s.study_accession,coalesce(s.biome,st.biome) AS biome,
            coalesce(s.biome_lineage,st.biome_lineage) AS biome_lineage,s.latitude,s.longitude,
            s.collection_date,s.country,s.environmental_material,s.analysis_count,st.title AS study_title
            FROM samples s LEFT JOIN studies st ON st.accession=s.study_accession
            WHERE s.accession ILIKE ? OR s.study_accession ILIKE ? OR coalesce(st.title,'') ILIKE ?
              OR coalesce(s.biome,st.biome,'') ILIKE ? OR coalesce(s.country,'') ILIKE ?
            ORDER BY CASE WHEN s.accession ILIKE ? THEN 0 ELSE 1 END,s.accession LIMIT ?"""
    else:
        sql="""SELECT accession,study_accession,biome,biome_lineage,latitude,longitude,collection_date,country,
            environmental_material,analysis_count,NULL AS study_title FROM samples
            WHERE accession ILIKE ? OR study_accession ILIKE ? OR coalesce(biome,'') ILIKE ? OR coalesce(country,'') ILIKE ?
            ORDER BY CASE WHEN accession ILIKE ? THEN 0 ELSE 1 END,accession LIMIT ?"""
    args=[term,term,term,term,term,f"{q.strip()}%",limit] if table_exists("studies") else [term,term,term,term,f"{q.strip()}%",limit]
    return {"query":q,"items":query(sql,args),"includes_samples_without_coordinates":True}

@app.get("/api/v1/samples/{accession}")
def sample_detail(accession: str):
    rows = query("SELECT * FROM samples WHERE accession = ?", [accession])
    if not rows: raise HTTPException(404, "Sample not found")
    detail = rows[0]
    if table_exists("studies"):
        studies=query("SELECT accession,title,description,biome,biome_lineage,updated_at,source_url FROM studies WHERE accession = ?",[detail.get("study_accession")])
        detail["study"] = studies[0] if studies else None
    detail["analysis_count"] = query("SELECT count(*) AS n FROM analyses WHERE sample_accession = ?", [accession])[0]["n"] if table_exists("analyses") else None
    detail["analyses_checked"] = bool(query("SELECT count(*) AS n FROM analysis_sync WHERE study_accession = ?", [detail.get("study_accession")])[0]["n"]) if table_exists("analysis_sync") else False
    return detail

@app.get("/api/v1/studies/{accession}")
def study_detail(accession: str):
    if not table_exists("studies"): raise HTTPException(404,"Study metadata has not been synchronized")
    rows=query("SELECT accession,title,description,biome,biome_lineage,updated_at,source_url FROM studies WHERE accession = ?",[accession])
    if not rows: raise HTTPException(404,"Study not found")
    row=rows[0]
    row["sample_count"]=query("SELECT count(*) AS n FROM samples WHERE study_accession = ?",[accession])[0]["n"]
    row["run_count"]=query("SELECT count(*) AS n FROM runs WHERE study_accession = ?",[accession])[0]["n"] if table_exists("runs") else 0
    row["analysis_count"]=query("SELECT count(*) AS n FROM analyses WHERE study_accession = ?",[accession])[0]["n"] if table_exists("analyses") else 0
    return row

@app.get("/api/v1/samples/{accession}/analyses")
def sample_analyses(accession: str):
    if not query("SELECT accession FROM samples WHERE accession = ?", [accession]): raise HTTPException(404, "Sample not found")
    if not table_exists("analyses"): return {"items": [], "checked": False, "message": "Analysis discovery has not been run for this dataset."}
    rows = query("SELECT a.accession, a.run_accession, a.study_accession, a.experiment_type, a.pipeline_family, a.pipeline_version, a.detail_checked_at, (SELECT count(*) FROM analysis_downloads d WHERE d.analysis_accession=a.accession) AS reported_downloads FROM analyses a WHERE sample_accession = ? ORDER BY accession", [accession])
    checked = bool(query("SELECT count(*) AS n FROM analysis_sync WHERE study_accession = ?", [query("SELECT study_accession FROM samples WHERE accession = ?",[accession])[0]["study_accession"]])[0]["n"]) if table_exists("analysis_sync") else False
    return {"items": rows, "checked": checked, "message": "Download metadata is discovered from MGnify analysis records; profile artifacts are not yet parsed."}

@app.get("/api/v1/analyses/{accession}/downloads")
def analysis_downloads(accession: str):
    if not table_exists("analyses"): raise HTTPException(404, "Analysis metadata has not been synchronized")
    if not query("SELECT accession FROM analyses WHERE accession = ?", [accession]): raise HTTPException(404, "Analysis not found")
    return {"items": query("SELECT file_type, download_type, download_group, alias, short_description, file_size_bytes, url, index_files FROM analysis_downloads WHERE analysis_accession = ? ORDER BY download_group, file_type", [accession]), "raw_sequence_files_excluded_from_profile_candidates": True}

@app.get("/api/v1/samples/{accession}/taxonomy")
def sample_taxonomy(accession: str, analysis_accession: str | None = None, reference_database: str | None = None, limit: int = Query(500, ge=1, le=5000)):
    if not query("SELECT accession FROM samples WHERE accession = ?", [accession]): raise HTTPException(404, "Sample not found")
    if not table_exists("taxonomy_observations"):
        return {"items": [], "profiles": [], "checked": False, "message": "No supported taxonomy artifacts have been parsed."}
    where=["sample_accession = ?"]; args: list[Any]=[accession]
    if analysis_accession:
        where.append("analysis_accession = ?"); args.append(analysis_accession)
    if reference_database:
        where.append("reference_database = ?"); args.append(reference_database)
    profiles=query(f"SELECT DISTINCT analysis_accession, run_accession, taxonomy_method, reference_database, value_semantics, source_url FROM taxonomy_observations WHERE {' AND '.join(where)} ORDER BY analysis_accession, reference_database",args)
    observations=query(f"SELECT analysis_accession, run_accession, taxonomy_method, reference_database, taxonomy_id, scientific_name, taxonomic_rank, taxonomic_lineage, reported_value, value_semantics, source_url FROM taxonomy_observations WHERE {' AND '.join(where)} ORDER BY reference_database, taxonomic_rank, scientific_name LIMIT ?",args+[limit])
    return {"items":observations,"profiles":profiles,"checked":True,"message":"Reported BIOM taxonomy-summary values; reference databases are returned separately and values are not normalized relative abundance."}

@app.get("/api/v1/samples/{accession}/functions")
def sample_functions(accession: str, analysis_accession: str | None = None, annotation_database: str | None = None, limit: int = Query(500, ge=1, le=5000)):
    if not query("SELECT accession FROM samples WHERE accession = ?", [accession]): raise HTTPException(404, "Sample not found")
    if not table_exists("functional_observations"):
        return {"items": [], "profiles": [], "checked": False, "message": "No supported V6 assembly function summaries have been parsed."}
    where=["sample_accession = ?"]; args: list[Any]=[accession]
    if analysis_accession:
        where.append("analysis_accession = ?"); args.append(analysis_accession)
    if annotation_database:
        where.append("annotation_database = ?"); args.append(annotation_database)
    profiles=query(f"SELECT DISTINCT analysis_accession,run_accession,annotation_method,annotation_database,value_semantics,source_url FROM functional_observations WHERE {' AND '.join(where)} ORDER BY annotation_database,analysis_accession",args)
    observations=query(f"SELECT analysis_accession,run_accession,annotation_method,annotation_database,function_id,function_name,functional_category,reported_value,value_semantics,source_url FROM functional_observations WHERE {' AND '.join(where)} ORDER BY annotation_database,function_id LIMIT ?",args+[limit])
    return {"items":observations,"profiles":profiles,"checked":True,"message":"Predicted assembly annotations are shown as source-reported counts or module completeness; they are not evidence of expression or metabolic activity."}

@app.get("/api/v1/analyses/{accession}/alpha-diversity")
def alpha_diversity(accession: str, reference_database: str, rank: str = Query("Genus",pattern="^(Domain|Superkingdom|Kingdom|Supergroup|Division|Subdivision|Phylum|Class|Order|Family|Genus|Species)$")):
    if not table_exists("taxonomy_observations"):
        raise HTTPException(404,"No taxonomy profiles have been parsed")
    if not query("SELECT accession FROM analyses WHERE accession = ?",[accession]):
        raise HTTPException(404,"Analysis not found")
    rows=query("""SELECT taxonomic_lineage,sum(reported_value) AS reported_count
        FROM taxonomy_observations WHERE analysis_accession = ? AND reference_database = ?
          AND taxonomic_rank = ? AND scientific_name <> 'Unclassified' AND reported_value > 0
        GROUP BY taxonomic_lineage ORDER BY taxonomic_lineage""",[accession,reference_database,rank])
    if not rows:
        return {"analysis_accession":accession,"reference_database":reference_database,"rank":rank,"observed_richness":0,"shannon":None,"simpson":None,"classified_count_total":0,"method":"MGnify V6 amplicon taxonomy summary","definition":"Observed taxa and Shannon/Simpson indices calculated from positive BIOM taxonomy-summary counts at the selected rank. Unclassified assignments are not included."}
    counts=[float(row["reported_count"]) for row in rows]
    total=sum(counts)
    proportions=[count/total for count in counts]
    import math
    shannon=-sum(p*math.log(p) for p in proportions if p>0)
    simpson=1-sum(p*p for p in proportions)
    return {"analysis_accession":accession,"reference_database":reference_database,"rank":rank,"observed_richness":len(counts),"shannon":shannon,"simpson":simpson,"classified_count_total":total,"method":"MGnify V6 amplicon taxonomy summary","definition":"Observed taxa and Shannon/Simpson indices calculated from positive BIOM taxonomy-summary counts at the selected rank. Unclassified assignments are not included; these describe the parsed profile, not the complete ecosystem."}

@app.get("/api/v1/community-comparison")
def community_comparison(sample_a: str, sample_b: str, reference_database: str, marker_region: str | None = None, rank: str = Query("Genus",pattern="^(Domain|Superkingdom|Kingdom|Supergroup|Division|Subdivision|Phylum|Class|Order|Family|Genus|Species)$")):
    if sample_a == sample_b: raise HTTPException(422,"Choose two different physical samples")
    if not table_exists("taxonomy_observations") or not table_exists("analysis_downloads"):
        raise HTTPException(404,"Parsed, method-compatible profiles are not available")
    for sample in (sample_a,sample_b):
        if not query("SELECT accession FROM samples WHERE accession = ?",[sample]): raise HTTPException(404,f"Sample {sample} not found")
    profile_sql = r"""SELECT DISTINCT t.analysis_accession,
        regexp_extract(d.alias,'^[^_]+_(.*)_asv_read_counts\.tsv$',1) AS marker_region
        FROM taxonomy_observations t JOIN analyses a ON a.accession=t.analysis_accession
        JOIN analysis_downloads d ON d.analysis_accession=a.accession
        WHERE t.sample_accession = ? AND t.reference_database = ? AND a.pipeline_version LIKE 'V6%'
          AND a.experiment_type='Amplicon' AND d.download_type='Statistics'
          AND d.alias LIKE '%_asv_read_counts.tsv'"""
    profiles_a=query(profile_sql,[sample_a,reference_database]); profiles_b=query(profile_sql,[sample_b,reference_database])
    if marker_region:
        profiles_a=[profile for profile in profiles_a if profile["marker_region"]==marker_region]
        profiles_b=[profile for profile in profiles_b if profile["marker_region"]==marker_region]
    if len(profiles_a)!=1 or len(profiles_b)!=1:
        raise HTTPException(409,{"message":"Each sample must have exactly one parsed V6 amplicon profile for this reference database with a reported ASV marker region.","sample_a_profiles":profiles_a,"sample_b_profiles":profiles_b})
    pa,pb=profiles_a[0],profiles_b[0]
    if not pa["marker_region"] or pa["marker_region"]!=pb["marker_region"]:
        raise HTTPException(422,{"message":"The selected profiles do not have the same reported amplicon marker/region.","sample_a_marker_region":pa["marker_region"],"sample_b_marker_region":pb["marker_region"]})
    def profile_counts(analysis: str):
        rows=query("""SELECT taxonomic_lineage,sum(reported_value) AS count FROM taxonomy_observations
            WHERE analysis_accession = ? AND reference_database = ? AND taxonomic_rank = ?
              AND scientific_name <> 'Unclassified' AND reported_value > 0 GROUP BY taxonomic_lineage""",[analysis,reference_database,rank])
        return {row["taxonomic_lineage"]:float(row["count"]) for row in rows}
    left,right=profile_counts(pa["analysis_accession"]),profile_counts(pb["analysis_accession"])
    if not left or not right: raise HTTPException(422,"Both profiles need positive classified counts at the selected rank")
    union=set(left)|set(right); shared=set(left)&set(right)
    denominator=sum(left.values())+sum(right.values())
    bray=sum(abs(left.get(t,0)-right.get(t,0)) for t in union)/denominator if denominator else None
    jaccard=len(shared)/len(union) if union else None
    return {"sample_a":sample_a,"sample_b":sample_b,"analysis_a":pa["analysis_accession"],"analysis_b":pb["analysis_accession"],"pipeline_version":"V6","experiment_type":"Amplicon","reference_database":reference_database,"marker_region":pa["marker_region"],"rank":rank,"shared_taxa":len(shared),"union_taxa":len(union),"bray_curtis_dissimilarity":bray,"jaccard_similarity":jaccard,"definition":"Calculated from BIOM taxonomy-summary counts for two V6 amplicon profiles with the same reference database and reported marker/region. Only taxa assigned at the selected rank are included; results describe the parsed profiles, not complete ecosystems."}

@app.get("/api/v1/timeseries")
def timeseries(habitat: str | None = None, q: str | None = Query(None,max_length=120)):
    values={"Marine":["marine"],"Freshwater":["freshwater"],"Soil":["soil"],"Terrestrial":["terrestrial"],"Polar":["polar","arctic","antarctic","permafrost","glacier","ice","snow"],"Hot springs":["hot spring","hot springs","geothermal"]}
    where=["s.collection_date IS NOT NULL","regexp_matches(s.collection_date, '^[0-9]{4}')"]; args: list[Any]=[]
    join=" samples s"
    biome_column="s.biome_lineage"
    if table_exists("studies"):
        join=" samples s LEFT JOIN studies st ON st.accession=s.study_accession"
        biome_column="coalesce(s.biome_lineage,st.biome_lineage,'')"
    if habitat and habitat!="All habitats":
        segments=values.get(habitat)
        if not segments: raise HTTPException(422,"Unsupported habitat filter")
        where.append("("+" OR ".join([f"list_contains(string_split(lower({biome_column}), ':'), ?)" for _ in segments])+")")
        args.extend(segments)
    if q and q.strip():
        term=f"%{q.strip()}%"
        searchable=["s.accession ILIKE ?","coalesce(s.study_accession,'') ILIKE ?","coalesce(s.biome,'') ILIKE ?","coalesce(s.country,'') ILIKE ?"]
        args.extend([term]*len(searchable))
        if table_exists("studies"):
            searchable.append("coalesce(st.title,'') ILIKE ?"); args.append(term)
        where.append("("+" OR ".join(searchable)+")")
    return {"items":query(f"SELECT substr(s.collection_date,1,4) AS year,count(*) AS samples FROM{join} WHERE {' AND '.join(where)} GROUP BY year ORDER BY year",args),"filters":{"habitat":habitat or "All habitats","query":q}}

@app.get("/api/v1/data-sources")
def sources():
    return {"sources": [{"name":"MGnify API v2", "role":"Primary source for study, sample, run and analysis records", "url":"https://www.ebi.ac.uk/metagenomics/api/v2/"}, {"name":"European Nucleotide Archive", "role":"Origin of much of the sample metadata exposed through MGnify", "url":"https://www.ebi.ac.uk/ena/browser/home"}]}

