"use client";
import { ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import { MapLibreOverlay } from "@deck.gl/maplibre";
import {
  Activity,
  ArrowDownUp,
  ChevronDown,
  ChevronLeft,
  CircleHelp,
  Compass,
  Database,
  Dna,
  Droplets,
  Globe2,
  Layers3,
  LocateFixed,
  Menu,
  Minus,
  Plus,
  Search,
  SlidersHorizontal,
  Sparkles,
  Waves,
  X,
} from "lucide-react";
import type { Map as MapLibreMap } from "maplibre-gl";
import * as maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useEffect, useMemo, useRef, useState } from "react";

maplibregl.setWorkerUrl(
  new URL(
    "maplibre-gl/dist/maplibre-gl-worker.mjs",
    import.meta.url,
  ).toString(),
);

type Sample = {
  accession: string;
  study_accession: string | null;
  study_title: string | null;
  biome: string | null;
  biome_lineage: string | null;
  latitude: number | null;
  longitude: number | null;
  collection_date: string | null;
  country: string | null;
  environmental_material: string | null;
  analysis_count: number | null;
};
type GeoSample = Sample & { latitude: number; longitude: number };
type MapPoint = GeoSample & { sample: GeoSample; sample_count: number };
type LinkedAnalysis = {
  accession: string;
  run_accession: string | null;
  experiment_type: string | null;
  pipeline_family: string | null;
  pipeline_version: string | null;
  detail_checked_at: string | null;
  reported_downloads: number;
};
type Taxon = {
  analysis_accession: string;
  taxonomy_method: string;
  reference_database: string;
  scientific_name: string;
  taxonomic_rank: string;
  taxonomic_lineage: string;
  reported_value: number;
  value_semantics: string;
};
type TaxonomyProfile = {
  analysis_accession: string;
  reference_database: string;
  taxonomy_method: string;
};
type AlphaMetric = {
  analysis_accession: string;
  reference_database: string;
  rank: string;
  observed_richness: number;
  shannon: number | null;
  simpson: number | null;
  classified_count_total: number;
  definition: string;
};
type CommunityComparison = {
  sample_a: string;
  sample_b: string;
  analysis_a: string;
  analysis_b: string;
  marker_region: string;
  reference_database: string;
  rank: string;
  shared_taxa: number;
  union_taxa: number;
  bray_curtis_dissimilarity: number;
  jaccard_similarity: number;
  definition: string;
};
type FunctionalRecord = {
  analysis_accession: string;
  annotation_method: string;
  annotation_database: string;
  function_id: string;
  function_name: string;
  functional_category: string | null;
  reported_value: number;
  value_semantics: string;
};
type Overview = {
  unique_samples: number;
  studies: number;
  georeferenced: number;
  with_analyses: number;
  analysis_records: number;
  taxonomy_observations: number;
  taxonomy_profiles: number;
  functional_observations: number;
  functional_profiles: number;
  checked_studies: number;
  analysis_discovery_complete: boolean;
  last_sample_sync: string | null;
  last_analysis_sync: string | null;
  last_profile_sync: string | null;
  last_function_sync: string | null;
  source: string;
};
const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";

function matchesHabitat(sample: Sample, habitat: string) {
  if (habitat === "All habitats") return true;
  const segments = (sample.biome_lineage || "")
    .split(":")
    .map((v) => v.trim().toLowerCase());
  if (habitat === "Marine") return segments.includes("marine");
  if (habitat === "Freshwater") return segments.includes("freshwater");
  if (habitat === "Soil") return segments.includes("soil");
  if (habitat === "Terrestrial") return segments.includes("terrestrial");
  if (habitat === "Polar")
    return [
      "polar",
      "arctic",
      "antarctic",
      "permafrost",
      "glacier",
      "ice",
      "snow",
    ].some((v) => segments.includes(v));
  if (habitat === "Hot springs")
    return ["hot spring", "hot springs", "geothermal"].some((v) =>
      segments.includes(v),
    );
  return false;
}

function aggregatePoints(samples: Sample[], zoom: number): MapPoint[] {
  const cell =
    zoom < 3
      ? 5
      : zoom < 4
        ? 2.5
        : zoom < 5
          ? 1.25
          : zoom < 6
            ? 0.5
            : zoom < 8
              ? 0.1
              : zoom < 11
                ? 0.02
                : 0.002;
  const geo = samples.filter(
    (s): s is GeoSample => s.latitude !== null && s.longitude !== null,
  );
  const groups = new Map<string, GeoSample[]>();
  for (const sample of geo) {
    const lonCell =
      cell / Math.max(0.2, Math.cos((sample.latitude * Math.PI) / 180));
    const key =
      zoom >= 12
        ? `${sample.latitude.toFixed(4)},${sample.longitude.toFixed(4)}`
        : `${Math.floor((sample.latitude + 90) / cell)},${Math.floor((sample.longitude + 180) / lonCell)}`;
    const group = groups.get(key) || [];
    group.push(sample);
    groups.set(key, group);
  }
  return [...groups.values()].map((group) => ({
    ...group[0],
    sample: group[0],
    sample_count: group.length,
    latitude: group.reduce((sum, s) => sum + s.latitude, 0) / group.length,
    longitude: group.reduce((sum, s) => sum + s.longitude, 0) / group.length,
  }));
}

export default function Home() {
  const mapNode = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const overlayRef = useRef<MapLibreOverlay | null>(null);
  const [samples, setSamples] = useState<Sample[]>([]);
  const [overview, setOverview] = useState<Overview>({
    unique_samples: 0,
    studies: 0,
    georeferenced: 0,
    with_analyses: 0,
    analysis_records: 0,
    taxonomy_observations: 0,
    taxonomy_profiles: 0,
    functional_observations: 0,
    functional_profiles: 0,
    checked_studies: 0,
    analysis_discovery_complete: false,
    last_sample_sync: null,
    last_analysis_sync: null,
    last_profile_sync: null,
    last_function_sync: null,
    source: "Local dataset",
  });
  const [linkedAnalyses, setLinkedAnalyses] = useState<LinkedAnalysis[]>([]);
  const [analysisChecked, setAnalysisChecked] = useState(false);
  const [taxa, setTaxa] = useState<Taxon[]>([]);
  const [taxonomyProfiles, setTaxonomyProfiles] = useState<TaxonomyProfile[]>(
    [],
  );
  const [alphaMetrics, setAlphaMetrics] = useState<AlphaMetric[]>([]);
  const [taxonomicRank, setTaxonomicRank] = useState("Genus");
  const [functions, setFunctions] = useState<FunctionalRecord[]>([]);
  const [compareA, setCompareA] = useState("");
  const [compareB, setCompareB] = useState("");
  const [compareDatabase, setCompareDatabase] = useState("SILVA-SSU");
  const [compareMarker, setCompareMarker] = useState("16S-V3-V4");
  const [compareRank, setCompareRank] = useState("Genus");
  const [comparison, setComparison] = useState<CommunityComparison | null>(
    null,
  );
  const [compareError, setCompareError] = useState("");
  const [comparing, setComparing] = useState(false);
  const [mapZoom, setMapZoom] = useState(1.5);
  const [selected, setSelected] = useState<Sample | null>(null);
  const [activeHabitat, setActiveHabitat] = useState("All habitats");
  const [panel, setPanel] = useState<
    "Explore" | "Taxonomy" | "Functions" | "Compare" | "Data"
  >("Explore");
  const [search, setSearch] = useState("");
  const [searchHits, setSearchHits] = useState<Sample[]>([]);
  const [busy, setBusy] = useState(true);
  const [apiOnline, setApiOnline] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [mapError, setMapError] = useState<string | null>(null);
  const [timeline, setTimeline] = useState<{ year: string; samples: number }[]>(
    [],
  );

  useEffect(() => {
    let alive = true;
    Promise.all([
      fetch(`${API}/overview`).then((r) => (r.ok ? r.json() : null)),
      fetch(`${API}/samples?limit=2000`).then((r) => (r.ok ? r.json() : null)),
    ])
      .then(([o, s]) => {
        if (!alive) return;
        if (o) {
          setOverview(o);
          setApiOnline(true);
        }
        if (s) setSamples(s.items || []);
      })
      .catch(() => {})
      .finally(() => alive && setBusy(false));
    return () => {
      alive = false;
    };
  }, []);
  useEffect(() => {
    let alive = true;
    const params = new URLSearchParams({ habitat: activeHabitat });
    if (search.trim()) params.set("q", search.trim());
    fetch(`${API}/timeseries?${params}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((v) => {
        if (alive && v) setTimeline(v.items || []);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [activeHabitat, search]);
  useEffect(() => {
    let alive = true;
    if (search.trim().length < 2) {
      setSearchHits([]);
      return;
    }
    const timer = setTimeout(
      () =>
        fetch(
          `${API}/search/samples?q=${encodeURIComponent(search.trim())}&limit=8`,
        )
          .then((r) => (r.ok ? r.json() : null))
          .then((v) => {
            if (alive && v) setSearchHits(v.items || []);
          })
          .catch(() => {}),
      220,
    );
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [search]);
  useEffect(() => {
    if (!mapNode.current || mapRef.current) return;
    try {
      const map = new maplibregl.Map({
        container: mapNode.current,
        style:
          "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
        center: [12, 20],
        zoom: 1.5,
        minZoom: 1.2,
        maxZoom: 15,
        attributionControl: false,
      });
      const overlay = new MapLibreOverlay({ interleaved: true, layers: [] });
      const onZoom = () => setMapZoom(map.getZoom());
      map.on("zoomend", onZoom);
      map.addControl(overlay as never);
      mapRef.current = map;
      overlayRef.current = overlay;
      return () => {
        map.off("zoomend", onZoom);
        map.remove();
        mapRef.current = null;
        overlayRef.current = null;
      };
    } catch (err) {
      console.warn("Map initialization failed:", err);
      setMapError("WebGL2 is required to display the interactive map.");
    }
  }, []);
  const visible = useMemo(
    () =>
      samples.filter(
        (s) =>
          matchesHabitat(s, activeHabitat) &&
          (!search ||
            `${s.accession} ${s.biome || ""} ${s.country || ""}`
              .toLowerCase()
              .includes(search.toLowerCase())),
      ),
    [samples, activeHabitat, search],
  );
  const mapPoints = useMemo(
    () => aggregatePoints(visible, mapZoom),
    [visible, mapZoom],
  );
  useEffect(() => {
    if (!overlayRef.current) return;
    const dots = new ScatterplotLayer<MapPoint>({
      id: "environmental-samples",
      data: mapPoints,
      getPosition: (d) => [d.longitude, d.latitude],
      getRadius: (d) =>
        d.sample_count > 1
          ? 50000 * (1 + Math.log2(d.sample_count))
          : selected?.accession === d.sample.accession
            ? 90000
            : 50000,
      radiusMinPixels: 3,
      radiusMaxPixels: 18,
      getFillColor: (d) =>
        d.sample_count > 1 ? [105, 232, 196, 215] : [102, 226, 194, 190],
      getLineColor: [182, 255, 232, 230],
      lineWidthMinPixels: 1,
      stroked: true,
      pickable: true,
      onClick: (info) => {
        const point = info.object;
        if (!point) return;
        if (point.sample_count > 1) {
          mapRef.current?.flyTo({
            center: [point.longitude, point.latitude],
            zoom: Math.min(mapZoom + 2, 12),
            duration: 500,
          });
          return;
        }
        setSelected(point.sample);
      },
    });
    const labels = new TextLayer<MapPoint>({
      id: "cluster-count-labels",
      data: mapZoom < 4 ? mapPoints.filter((p) => p.sample_count > 1) : [],
      getPosition: (d) => [d.longitude, d.latitude],
      getText: (d) => String(d.sample_count),
      getSize: 11,
      getColor: [220, 255, 245],
      fontFamily: "DM Mono",
      billboard: true,
      pickable: false,
    });
    overlayRef.current.setProps({ layers: [dots, labels] });
  }, [mapPoints, mapZoom, selected]);
  useEffect(() => {
    let alive = true;
    setLinkedAnalyses([]);
    setAnalysisChecked(false);
    setTaxa([]);
    setTaxonomyProfiles([]);
    setAlphaMetrics([]);
    setFunctions([]);
    if (!selected) return;
    fetch(`${API}/samples/${encodeURIComponent(selected.accession)}/analyses`)
      .then((r) => (r.ok ? r.json() : null))
      .then((v) => {
        if (alive && v) {
          setLinkedAnalyses(v.items || []);
          setAnalysisChecked(!!v.checked);
        }
      })
      .catch(() => {});
    fetch(
      `${API}/samples/${encodeURIComponent(selected.accession)}/taxonomy?limit=100`,
    )
      .then((r) => (r.ok ? r.json() : null))
      .then((v) => {
        if (alive && v) {
          setTaxa(v.items || []);
          setTaxonomyProfiles(v.profiles || []);
        }
      })
      .catch(() => {});
    fetch(
      `${API}/samples/${encodeURIComponent(selected.accession)}/functions?limit=100`,
    )
      .then((r) => (r.ok ? r.json() : null))
      .then((v) => {
        if (alive && v) setFunctions(v.items || []);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [selected]);
  useEffect(() => {
    let alive = true;
    setAlphaMetrics([]);
    if (!selected || taxonomyProfiles.length === 0) return;
    Promise.all(
      taxonomyProfiles.map((profile) => {
        const params = new URLSearchParams({
          reference_database: profile.reference_database,
          rank: taxonomicRank,
        });
        return fetch(
          `${API}/analyses/${encodeURIComponent(profile.analysis_accession)}/alpha-diversity?${params}`,
        ).then((r) => (r.ok ? r.json() : null));
      }),
    )
      .then((values) => {
        if (alive) setAlphaMetrics(values.filter((v): v is AlphaMetric => !!v));
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [selected, taxonomyProfiles, taxonomicRank]);

  const habitats = [
    "All habitats",
    "Marine",
    "Freshwater",
    "Soil",
    "Terrestrial",
    "Polar",
    "Hot springs",
  ];
  return (
    <main className="shell">
      <header className="topbar">
        <button
          className="mobile-menu icon-button"
          aria-label="Open filters"
          onClick={() => setMobileOpen(!mobileOpen)}
        >
          <Menu size={18} />
        </button>
        <a className="brand" href="#">
          <span className="brand-mark">
            <Globe2 size={19} />
          </span>
          <span>
            <strong>MICROBIAL ATLAS</strong>
            <small>ENVIRONMENTAL BIOSPHERE</small>
          </span>
        </a>
        <div className="top-divider" />
        <nav className="primary-nav">
          {(
            ["Explore", "Taxonomy", "Functions", "Compare", "Data"] as const
          ).map((n, i) => (
            <button
              key={n}
              className={panel === n ? "nav-link active" : "nav-link"}
              onClick={() => setPanel(n)}
            >
              {n === "Explore" ? (
                <Compass size={15} />
              ) : n === "Taxonomy" ? (
                <Dna size={15} />
              ) : n === "Functions" ? (
                <Activity size={15} />
              ) : n === "Compare" ? (
                <ArrowDownUp size={15} />
              ) : (
                <Database size={15} />
              )}
              <span>{n}</span>
            </button>
          ))}
        </nav>
        <div className="top-right">
          <span className={`status ${apiOnline ? "online" : ""}`}>
            <i />
            {apiOnline ? "LOCAL DATA CONNECTED" : "AWAITING LOCAL DATA"}
          </span>
          <button
            className="icon-button help"
            aria-label="About data"
            title="Only source-derived records are shown"
          >
            <CircleHelp size={17} />
          </button>
        </div>
      </header>
      <div className="workspace">
        <aside className={`filters ${mobileOpen ? "mobile-open" : ""}`}>
          <div className="filter-head">
            <div>
              <span className="eyebrow">ATLAS CONTROLS</span>
              <h1>Explore</h1>
            </div>
            <button
              className="icon-button mobile-close"
              onClick={() => setMobileOpen(false)}
              aria-label="Close filters"
            >
              <X size={17} />
            </button>
            <button
              className="icon-button collapse"
              aria-label="Collapse filters"
            >
              <ChevronLeft size={18} />
            </button>
          </div>
          <label className="searchbox">
            <Search size={15} />
            <input
              placeholder="Search samples, studies…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
            <kbd>⌘ K</kbd>
          </label>
          {search.trim().length >= 2 && (
            <div className="search-results">
              {searchHits.map((sample) => (
                <button
                  key={sample.accession}
                  onClick={() => {
                    setSelected(sample);
                    setPanel("Explore");
                    if (sample.latitude !== null && sample.longitude !== null)
                      mapRef.current?.flyTo({
                        center: [sample.longitude, sample.latitude],
                        zoom: 7,
                        duration: 500,
                      });
                  }}
                >
                  <b>{sample.accession}</b>
                  <span>
                    {sample.biome || "Biome not reported"}
                    {sample.study_title ? ` · ${sample.study_title}` : ""}
                  </span>
                  <small>
                    {sample.latitude === null || sample.longitude === null
                      ? "Coordinates not reported · sample remains searchable"
                      : "Source coordinates available"}
                  </small>
                </button>
              ))}
              {searchHits.length === 0 && (
                <p>
                  {busy
                    ? "Searching source records…"
                    : "No matching sample or study"}
                </p>
              )}
            </div>
          )}
          <div className="section-title">
            <span>HABITAT</span>
            <button className="subtle-button">
              <SlidersHorizontal size={13} /> Filters
            </button>
          </div>
          <div className="habitats">
            {habitats.map((h, i) => (
              <button
                key={h}
                className={`habitat ${activeHabitat === h ? "selected" : ""}`}
                onClick={() => setActiveHabitat(h)}
              >
                <span className="hab-icon">
                  {i === 1 ? (
                    <Waves size={15} />
                  ) : i === 2 ? (
                    <Droplets size={15} />
                  ) : i === 0 ? (
                    <Globe2 size={15} />
                  ) : (
                    <Sparkles size={15} />
                  )}
                </span>
                <span>{h}</span>
                {h === "All habitats" && (
                  <small>
                    {(overview.unique_samples ?? 0).toLocaleString()}
                  </small>
                )}
              </button>
            ))}
          </div>
          <div className="divider" />
          <div className="section-title">
            <span>ENVIRONMENT</span>
          </div>
          <div className="filter-note">
            Temperature, depth and chemistry are not yet present in the imported
            metadata.
          </div>
          <div className="divider" />
          <div className="section-title">
            <span>ANALYSIS TYPE</span>
          </div>
          <div className="filter-note">
            {overview.taxonomy_profiles || overview.functional_profiles
              ? `${(overview.taxonomy_profiles ?? 0).toLocaleString()} V6 amplicon taxonomy profiles and ${(overview.functional_profiles ?? 0).toLocaleString()} functional profiles parsed. Profiles remain separate by reference database.`
              : "No processed taxonomic or functional profiles have been imported yet."}
          </div>
          <div className="filter-foot">
            <span>DATASET STATUS</span>
            <div>
              <span className={`dot ${apiOnline ? "good" : ""}`} />
              {apiOnline
                ? (overview.unique_samples ?? 0).toLocaleString() +
                  " samples loaded"
                : "No local records loaded"}
            </div>
            <small>MGnify API v2 · source and methods retained</small>
          </div>
        </aside>
        <section className="map-stage">
          <div ref={mapNode} className="map" />
          <div className="map-vignette" />
          <div className="map-head">
            <div className="map-title">
              <span className="eyebrow">
                {panel === "Explore"
                  ? "GLOBAL OVERVIEW"
                  : panel === "Data"
                    ? "DATA COVERAGE"
                    : `${panel.toUpperCase()} EXPLORER`}
              </span>
              <h2>
                {panel === "Explore"
                  ? "Environmental microbiomes"
                  : panel === "Data"
                    ? "Dataset coverage"
                    : panel === "Taxonomy"
                      ? "Taxonomic profiles"
                      : panel === "Functions"
                        ? "Functional annotations"
                        : "Community comparison"}
              </h2>
              <p>
                {panel === "Explore"
                  ? "Explore where microbial life has been sampled across the biosphere."
                  : panel === "Data"
                    ? "Inspect what the current local dataset contains and where records are incomplete."
                    : panel === "Taxonomy"
                      ? "Inspect parsed V6 amplicon taxonomy profiles and alpha diversity metrics."
                      : panel === "Functions"
                        ? "Inspect parsed V6 assembly functional summaries (Pfam, KEGG Orthology, InterPro)."
                        : "Compare compatible V6 amplicon profiles using Bray–Curtis dissimilarity and Jaccard similarity."}
              </p>
            </div>
            <button className="layer-button">
              <Layers3 size={15} /> Map layers <ChevronDown size={13} />
            </button>
          </div>
          <div className="map-legend">
            <span className="legend-dot" /> Verified sample coordinates{" "}
            <span className="legend-line" />{" "}
            {mapZoom < 12
              ? "Clusters aggregate physical samples by location; larger circles and labels show their counts. Click a cluster to zoom."
              : "Nearby samples remain grouped until a closer zoom level."}
          </div>
          {!busy && panel === "Explore" && visible.length === 0 && (
            <div className="map-empty">
              <div className="empty-orb">
                <Globe2 size={25} />
              </div>
              <span className="eyebrow">READY TO EXPLORE</span>
              <h3>
                {apiOnline
                  ? "No samples in this view"
                  : "Connect a local dataset"}
              </h3>
              <p>
                {apiOnline
                  ? "Try a different habitat filter or reset your search."
                  : "Start the API and run the MGnify sync command to load verified environmental samples."}
              </p>
              {!apiOnline && <code>npm run ingest -- --limit 500</code>}
            </div>
          )}
          {!busy && panel !== "Explore" && (
            <div className="mode-panel">
              <span className="eyebrow">
                {panel === "Data"
                  ? "CURRENT LOCAL DATASET"
                  : "ANALYSIS RESULTS"}
              </span>
              {panel === "Data" ? (
                <>
                  <h3>Coverage you can verify</h3>
                  <p>
                    Counts below come from local source records. A sample is
                    counted once by accession, regardless of the number of
                    sequencing runs.
                  </p>
                  <div className="coverage-grid">
                    <div>
                      <b>{(overview.unique_samples ?? 0).toLocaleString()}</b>
                      <span>unique samples</span>
                    </div>
                    <div>
                      <b>{(overview.studies ?? 0).toLocaleString()}</b>
                      <span>studies</span>
                    </div>
                    <div>
                      <b>{(overview.georeferenced ?? 0).toLocaleString()}</b>
                      <span>valid coordinates</span>
                    </div>
                    <div>
                      <b>
                        {(
                          (overview.unique_samples ?? 0) -
                          (overview.georeferenced ?? 0)
                        ).toLocaleString()}
                      </b>
                      <span>without coordinates</span>
                    </div>
                    <div>
                      <b>
                        {(overview.taxonomy_profiles ?? 0).toLocaleString()}
                      </b>
                      <span>taxonomy profiles parsed</span>
                    </div>
                    <div>
                      <b>
                        {(overview.taxonomy_observations ?? 0).toLocaleString()}
                      </b>
                      <span>taxonomy rows</span>
                    </div>
                    <div>
                      <b>
                        {(overview.functional_profiles ?? 0).toLocaleString()}
                      </b>
                      <span>functional profiles parsed</span>
                    </div>
                    <div>
                      <b>
                        {(
                          overview.functional_observations ?? 0
                        ).toLocaleString()}
                      </b>
                      <span>functional rows</span>
                    </div>
                    <div>
                      <b>{(overview.analysis_records ?? 0).toLocaleString()}</b>
                      <span>analysis records linked</span>
                    </div>
                  </div>
                  <p className="mode-foot">
                    Analysis discovery checked{" "}
                    {(overview.checked_studies ?? 0).toLocaleString()} of{" "}
                    {(overview.studies ?? 0).toLocaleString()} sample studies
                    {overview.analysis_discovery_complete
                      ? ""
                      : " · discovery is incomplete"}
                    . Taxonomy and functional records retain analysis and
                    database provenance. Functional counts are predicted
                    annotations, not observed activity. Last sample sync:{" "}
                    {overview.last_sample_sync
                      ? new Date(overview.last_sample_sync).toLocaleString()
                      : "not recorded"}
                    . Missing values are not imputed.
                  </p>
                </>
              ) : (
                <>
                  <h3>
                    {panel === "Taxonomy"
                      ? selected
                        ? `Taxonomy for ${selected.accession}`
                        : "Select a sample to inspect parsed taxonomy"
                      : panel === "Functions"
                        ? selected
                          ? `Functional annotations for ${selected.accession}`
                          : overview.functional_profiles
                            ? "Select a sample to inspect parsed annotations"
                            : "No functional profiles parsed yet"
                        : "Community comparison"}
                  </h3>
                  <p>
                    {panel === "Taxonomy"
                      ? "Taxonomy rows appear in the sample inspector and remain separated by analysis and reference database. Current counts are source-reported BIOM summary values, not normalized abundance."
                      : panel === "Functions"
                        ? "V6 assembly annotation summaries are shown by database in the sample inspector. Counts are predicted protein annotations, while KEGG module values are completeness percentages; neither indicates expression or activity."
                        : "Community comparisons require compatible analysis methods, reference databases, and feature semantics. The current data has not been combined across profiles."}
                  </p>
                  <code>
                    {(overview.analysis_records ?? 0).toLocaleString()} linked
                    analyses ·{" "}
                    {(overview.taxonomy_profiles ?? 0).toLocaleString()}{" "}
                    taxonomy profiles ·{" "}
                    {(overview.functional_profiles ?? 0).toLocaleString()}{" "}
                    functional profiles
                  </code>
                  {panel === "Taxonomy" && !selected && (
                    <div className="compare-selected" style={{ marginTop: 12 }}>
                      <button
                        type="button"
                        className="subtle-button"
                        onClick={() => {
                          fetch(`${API}/samples/SAMEA115409203`)
                            .then((r) => (r.ok ? r.json() : null))
                            .then((s) => s && setSelected(s));
                        }}
                      >
                        Inspect example taxonomy sample (SAMEA115409203)
                      </button>
                    </div>
                  )}
                  {panel === "Functions" && !selected && (
                    <div className="compare-selected" style={{ marginTop: 12 }}>
                      <button
                        type="button"
                        className="subtle-button"
                        onClick={() => {
                          fetch(`${API}/samples/SAMN05518500`)
                            .then((r) => (r.ok ? r.json() : null))
                            .then((s) => s && setSelected(s));
                        }}
                      >
                        Inspect example functional sample (SAMN05518500)
                      </button>
                    </div>
                  )}
                  {panel === "Compare" && (
                    <form
                      className="compare-form"
                      onSubmit={(e) => {
                        e.preventDefault();
                        setComparing(true);
                        setCompareError("");
                        setComparison(null);
                        const params = new URLSearchParams({
                          sample_a: compareA.trim(),
                          sample_b: compareB.trim(),
                          reference_database: compareDatabase,
                          marker_region: compareMarker,
                          rank: compareRank,
                        });
                        fetch(`${API}/community-comparison?${params}`)
                          .then(async (r) => {
                            const body: any = await r.json();
                            if (!r.ok)
                              throw new Error(
                                typeof body.detail === "string"
                                  ? body.detail
                                  : body.detail?.message ||
                                      "No compatible profile pair was found.",
                              );
                            setComparison(body);
                          })
                          .catch((error) =>
                            setCompareError(
                              error.message ||
                                "Comparison could not be calculated.",
                            ),
                          )
                          .finally(() => setComparing(false));
                      }}
                    >
                      <div
                        className="compare-selected"
                        style={{ marginBottom: 10 }}
                      >
                        <button
                          type="button"
                          className="subtle-button"
                          onClick={() => {
                            setCompareA("SAMEA115409238");
                            setCompareB("SAMEA115409250");
                            setCompareDatabase("SILVA-SSU");
                            setCompareMarker("16S-V3-V4");
                            setCompareRank("Genus");
                          }}
                        >
                          Load example pair (SAMEA115409238 vs SAMEA115409250)
                        </button>
                      </div>
                      <label>
                        Sample A accession
                        <input
                          value={compareA}
                          onChange={(e) => setCompareA(e.target.value)}
                          placeholder="SAMEA…"
                          required
                          minLength={3}
                        />
                      </label>
                      <label>
                        Sample B accession
                        <input
                          value={compareB}
                          onChange={(e) => setCompareB(e.target.value)}
                          placeholder="SAMEA…"
                          required
                          minLength={3}
                        />
                      </label>
                      <div className="compare-selects">
                        <label>
                          Reference
                          <select
                            value={compareDatabase}
                            onChange={(e) => setCompareDatabase(e.target.value)}
                          >
                            <option>SILVA-SSU</option>
                            <option>SILVA-LSU</option>
                            <option>PR2</option>
                            <option>ITSoneDB</option>
                          </select>
                        </label>
                        <label>
                          Rank
                          <select
                            value={compareRank}
                            onChange={(e) => setCompareRank(e.target.value)}
                          >
                            <option>Class</option>
                            <option>Order</option>
                            <option>Family</option>
                            <option>Genus</option>
                            <option>Species</option>
                          </select>
                        </label>
                        <label>
                          Marker region
                          <select
                            value={compareMarker}
                            onChange={(e) => setCompareMarker(e.target.value)}
                          >
                            <option>16S-V3-V4</option>
                            <option>16S-V1-V3</option>
                          </select>
                        </label>
                      </div>
                      {selected && (
                        <div className="compare-selected">
                          <button
                            type="button"
                            className="subtle-button"
                            onClick={() => setCompareA(selected.accession)}
                          >
                            Use selected sample as A
                          </button>
                          <button
                            type="button"
                            className="subtle-button"
                            onClick={() => setCompareB(selected.accession)}
                          >
                            Use selected sample as B
                          </button>
                        </div>
                      )}
                      <button className="inspect-action" disabled={comparing}>
                        {comparing
                          ? "Comparing compatible profiles…"
                          : "Compare profiles"}
                      </button>
                      {compareError && (
                        <p className="compare-error">{compareError}</p>
                      )}
                      {comparison && (
                        <div className="analysis-card">
                          <b>
                            {comparison.marker_region} ·{" "}
                            {comparison.reference_database} · {comparison.rank}
                          </b>
                          <small>
                            Bray–Curtis dissimilarity:{" "}
                            {comparison.bray_curtis_dissimilarity.toFixed(3)} ·
                            Jaccard similarity:{" "}
                            {comparison.jaccard_similarity.toFixed(3)}
                          </small>
                          <small>
                            {comparison.shared_taxa} shared taxa of{" "}
                            {comparison.union_taxa} · {comparison.analysis_a} vs{" "}
                            {comparison.analysis_b}
                          </small>
                          <small>
                            Scores describe matched parsed profiles only, not
                            complete ecosystems.
                          </small>
                        </div>
                      )}
                    </form>
                  )}
                </>
              )}
            </div>
          )}
          {visible.length > 0 && (
            <div className="map-count">
              <span className="live-dot" />
              {visible.length.toLocaleString()} georeferenced samples
            </div>
          )}
          <div className="zoom-tools">
            <button
              aria-label="Zoom in"
              onClick={() => mapRef.current?.zoomIn()}
            >
              <Plus size={17} />
            </button>
            <button
              aria-label="Zoom out"
              onClick={() => mapRef.current?.zoomOut()}
            >
              <Minus size={17} />
            </button>
            <button
              aria-label="Reset map"
              onClick={() =>
                mapRef.current?.flyTo({ center: [12, 20], zoom: 1.5 })
              }
            >
              <LocateFixed size={16} />
            </button>
          </div>
          <div className="timeline">
            <div className="timeline-top">
              <div>
                <span className="eyebrow">COLLECTION TIMELINE</span>
                <span className="timeline-range">
                  {timeline.length
                    ? `${timeline[0].year} — ${timeline[timeline.length - 1].year}`
                    : "No dated records"}
                </span>
              </div>
              <button className="subtle-button">
                <span className="live-dot" />{" "}
                {timeline.reduce((n, d) => n + d.samples, 0).toLocaleString()}{" "}
                dated samples
              </button>
            </div>
            <div className="timeline-chart">
              <div className="chart-grid">
                <i />
                <i />
                <i />
              </div>
              {timeline.length > 0 && (
                <>
                  <svg
                    viewBox={`0 0 ${Math.max(timeline.length - 1, 1) * 24} 64`}
                    preserveAspectRatio="none"
                    role="img"
                    aria-label="Annual counts of source records with collection dates"
                  >
                    <polyline
                      points={timeline
                        .map(
                          (d, i) =>
                            `${i * 24},${60 - (d.samples / Math.max(...timeline.map((x) => x.samples))) * 53}`,
                        )
                        .join(" ")}
                      fill="none"
                      stroke="#62d8b9"
                      strokeWidth="1.5"
                      vectorEffect="non-scaling-stroke"
                    />
                  </svg>
                  <div className="years">
                    <span>{timeline[0].year}</span>
                    <span>
                      {timeline[Math.floor(timeline.length / 2)].year}
                    </span>
                    <span>{timeline[timeline.length - 1].year}</span>
                  </div>
                </>
              )}
            </div>
          </div>
          <div className="map-credit">
            © CARTO · © OpenStreetMap contributors
          </div>
        </section>
        {selected && (
          <aside className="inspector">
            <div className="inspect-top">
              <div>
                <span className="eyebrow">ENVIRONMENTAL SAMPLE</span>
                <strong className="sample-id">{selected.accession}</strong>
              </div>
              <button
                className="icon-button"
                onClick={() => setSelected(null)}
                aria-label="Close sample panel"
              >
                <X size={17} />
              </button>
            </div>
            <div className="inspect-body">
              <span className="tag">
                {selected.biome || "Biome not reported"}
              </span>
              <h2>
                {selected.environmental_material ||
                  selected.biome ||
                  "Environmental sample"}
              </h2>
              <p className="muted">
                {selected.country || "Country not reported"}
                {selected.collection_date
                  ? ` · ${selected.collection_date}`
                  : ""}
              </p>
              {selected.latitude !== null && selected.longitude !== null ? (
                <div className="coord">
                  <span>COORDINATES</span>
                  <b>
                    {selected.latitude.toFixed(4)}°,{" "}
                    {selected.longitude.toFixed(4)}°
                  </b>
                </div>
              ) : (
                <div className="coord">
                  <span>COORDINATES</span>
                  <b>Not reported · not mapped</b>
                </div>
              )}
              <div className="divider" />
              <span className="eyebrow">RECORD CONTEXT</span>
              <div className="detail-row">
                <span>Study</span>
                <b>
                  {selected.study_title ||
                    selected.study_accession ||
                    "Not linked"}
                </b>
              </div>
              {selected.study_title && (
                <div className="detail-row">
                  <span>Study accession</span>
                  <b>{selected.study_accession}</b>
                </div>
              )}
              <div className="detail-row">
                <span>Linked analyses</span>
                <b>{selected.analysis_count ?? "Not checked"}</b>
              </div>
              <div className="detail-row">
                <span>Source</span>
                <b>MGnify API v2</b>
              </div>
              <p className="integrity-note">
                A sample is a physical specimen. Runs and analyses are linked
                records and are not counted as separate samples. Linked analysis
                records do not imply profile artifacts have been parsed.
              </p>
              <div className="divider" />
              <span className="eyebrow">PROCESSED ANALYSES</span>
              {!analysisChecked && (
                <p className="muted">
                  Analysis discovery has not been checked for this study.
                </p>
              )}
              {analysisChecked && linkedAnalyses.length === 0 && (
                <p className="muted">
                  No linked analyses were found for this sample.
                </p>
              )}
              {linkedAnalyses.map((a) => (
                <div className="analysis-card" key={a.accession}>
                  <b>{a.accession}</b>
                  <small>
                    {[
                      a.experiment_type,
                      a.pipeline_version && `Pipeline ${a.pipeline_version}`,
                      a.run_accession && `Run ${a.run_accession}`,
                    ]
                      .filter(Boolean)
                      .join(" · ") || "Method not reported"}
                  </small>
                  <small>
                    {a.detail_checked_at
                      ? `${a.reported_downloads} source-reported download records`
                      : "Download metadata not checked"}
                  </small>
                </div>
              ))}
              <div className="divider" />
              <span className="eyebrow">TAXONOMIC OBSERVATIONS</span>
              {taxa.length === 0 && (
                <p className="muted">
                  No supported taxonomy profiles parsed for this sample yet.
                </p>
              )}
              {taxa.slice(0, 20).map((t, i) => (
                <div
                  className="analysis-card"
                  key={`${t.analysis_accession}-${t.reference_database}-${t.taxonomic_lineage}-${i}`}
                >
                  <b>
                    {t.scientific_name} · {t.taxonomic_rank}
                  </b>
                  <small>
                    {t.reference_database} · {t.analysis_accession}
                  </small>
                  <small>
                    {t.reported_value.toLocaleString()} reported BIOM count; not
                    normalized abundance
                  </small>
                </div>
              ))}
              <div className="diversity-control">
                <label htmlFor="diversity-rank">Alpha diversity rank</label>
                <select
                  id="diversity-rank"
                  value={taxonomicRank}
                  onChange={(e) => setTaxonomicRank(e.target.value)}
                >
                  <option>Class</option>
                  <option>Order</option>
                  <option>Family</option>
                  <option>Genus</option>
                  <option>Species</option>
                </select>
              </div>
              {alphaMetrics.length === 0 && (
                <p className="muted">
                  No positive assigned counts at {taxonomicRank.toLowerCase()}{" "}
                  rank in parsed profiles.
                </p>
              )}
              {alphaMetrics.map((m) => (
                <div
                  className="analysis-card"
                  key={`${m.analysis_accession}-${m.reference_database}`}
                >
                  <b>
                    {m.reference_database} · {m.analysis_accession}
                  </b>
                  <small>
                    Observed richness {m.observed_richness} · Shannon{" "}
                    {m.shannon === null ? "n/a" : m.shannon.toFixed(3)} ·
                    Simpson {m.simpson === null ? "n/a" : m.simpson.toFixed(3)}
                  </small>
                  <small>
                    {m.classified_count_total.toLocaleString()} reported counts
                    at {m.rank.toLowerCase()} rank · calculated within this
                    profile
                  </small>
                </div>
              ))}
              <p className="integrity-note">
                Diversity describes reported assigned taxa in each parsed
                profile, not the complete ecosystem. Profiles remain separate by
                analysis and reference database.
              </p>
              <div className="divider" />
              <span className="eyebrow">PREDICTED FUNCTIONAL ANNOTATIONS</span>
              {functions.length === 0 && (
                <p className="muted">
                  No supported V6 assembly function summaries parsed for this
                  sample yet.
                </p>
              )}
              {functions.slice(0, 20).map((f, i) => (
                <div
                  className="analysis-card"
                  key={`${f.analysis_accession}-${f.annotation_database}-${f.function_id}-${i}`}
                >
                  <b>{f.function_name}</b>
                  <small>
                    {f.annotation_database} · {f.function_id}
                    {f.functional_category ? ` · ${f.functional_category}` : ""}
                  </small>
                  <small>
                    {f.reported_value.toLocaleString()} · {f.value_semantics}
                  </small>
                </div>
              ))}
            </div>
            <button
              className="inspect-action"
              onClick={() => setPanel("Taxonomy")}
            >
              Inspect available profiles <span>↗</span>
            </button>
          </aside>
        )}
      </div>
    </main>
  );
}
