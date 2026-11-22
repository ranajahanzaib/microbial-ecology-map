import tempfile
import unittest
from pathlib import Path

import duckdb
from fastapi.testclient import TestClient

from backend import app as api


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        api.DB = Path(self.temp.name) / "fixture.duckdb"
        with duckdb.connect(str(api.DB)) as con:
            con.execute("CREATE TABLE samples (accession VARCHAR, study_accession VARCHAR, biome VARCHAR, biome_lineage VARCHAR, latitude DOUBLE, longitude DOUBLE, collection_date VARCHAR, country VARCHAR, environmental_material VARCHAR, analysis_count BIGINT, source_url VARCHAR, source_checksum VARCHAR)")
            con.execute("INSERT INTO samples VALUES ('SAMEA_TEST', 'MGYS_TEST', 'Marine', 'root:Environmental:Aquatic:Marine', 10, 20, '2020-05-01', 'Testland', 'seawater', 0, 'https://example.invalid/sample', 'fixture')")
            con.execute("CREATE TABLE studies (accession VARCHAR,title VARCHAR,description VARCHAR,biome VARCHAR,biome_lineage VARCHAR,updated_at VARCHAR,source_url VARCHAR,source_checksum VARCHAR,source_json JSON)")
            con.execute("INSERT INTO studies VALUES ('MGYS_TEST','Marine test study','Fixture description','Marine','root:Environmental:Aquatic:Marine',NULL,'https://example.invalid/study','hash','{}')")
            con.execute("CREATE TABLE analyses (accession VARCHAR, run_accession VARCHAR, sample_accession VARCHAR, study_accession VARCHAR, experiment_type VARCHAR, pipeline_version VARCHAR, pipeline_family VARCHAR, source_url VARCHAR, source_json JSON, detail_checked_at TIMESTAMP)")
            con.execute("CREATE TABLE analysis_downloads (analysis_accession VARCHAR, file_type VARCHAR, download_type VARCHAR, download_group VARCHAR, alias VARCHAR, short_description VARCHAR, file_size_bytes BIGINT, url VARCHAR, index_files JSON)")
            con.execute("CREATE TABLE analysis_sync (study_accession VARCHAR, checked_at TIMESTAMP, analysis_count BIGINT, details_checked BIGINT)")
            con.execute("CREATE TABLE taxonomy_observations (observation_key VARCHAR, analysis_accession VARCHAR, sample_accession VARCHAR, run_accession VARCHAR, taxonomy_method VARCHAR, reference_database VARCHAR, taxonomy_id VARCHAR, scientific_name VARCHAR, taxonomic_rank VARCHAR, taxonomic_lineage VARCHAR, reported_value DOUBLE, value_semantics VARCHAR, source_url VARCHAR, source_checksum VARCHAR, parser_version VARCHAR)")
            con.execute("INSERT INTO analyses VALUES ('MGYA_TEST','ERR_TEST','SAMEA_TEST','MGYS_TEST','Amplicon','V6.0',NULL,'https://example.invalid/analysis','{}',NULL)")
            con.execute("INSERT INTO analysis_sync VALUES ('MGYS_TEST',now(),1,0)")
            con.execute("INSERT INTO taxonomy_observations VALUES ('obs1','MGYA_TEST','SAMEA_TEST','ERR_TEST','MGnify V6 amplicon taxonomy summary','SILVA-SSU','123','Pseudomonas','Genus','sk__Bacteria;p__Proteobacteria;g__Pseudomonas',12,'BIOM taxonomy-summary count as reported; not normalized abundance','https://example.invalid/profile','hash','0.2.0')")
            con.execute("CREATE TABLE functional_observations (observation_key VARCHAR,analysis_accession VARCHAR,sample_accession VARCHAR,run_accession VARCHAR,annotation_method VARCHAR,annotation_database VARCHAR,function_id VARCHAR,function_name VARCHAR,functional_category VARCHAR,reported_value DOUBLE,value_semantics VARCHAR,details_json JSON,source_url VARCHAR,source_checksum VARCHAR,parser_version VARCHAR)")
            con.execute("INSERT INTO functional_observations VALUES ('fun1','MGYA_TEST','SAMEA_TEST','ERR_TEST','MGnify V6 assembly annotation','KEGG Orthology','K00033','Glucose-6-phosphate isomerase',NULL,20,'Reported count; predicted annotation', '{}','https://example.invalid/function','hash','0.3.0')")
        self.client = TestClient(api.app)

    def tearDown(self):
        self.temp.cleanup()

    def test_overview_uses_unique_physical_samples(self):
        response = self.client.get("/api/v1/overview")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["unique_samples"], 1)
        self.assertEqual(response.json()["georeferenced"], 1)

    def test_bounding_box_and_invalid_bbox(self):
        response = self.client.get("/api/v1/samples", params={"bbox": "19,9,21,11"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["accession"], "SAMEA_TEST")
        self.assertEqual(self.client.get("/api/v1/samples", params={"bbox": "west,0,1,1"}).status_code, 422)

    def test_overview_counts_linked_analyses_not_physical_samples(self):
        overview = self.client.get("/api/v1/overview").json()
        self.assertEqual(overview["unique_samples"], 1)
        self.assertEqual(overview["analysis_records"], 1)
        self.assertEqual(overview["with_analyses"], 1)

    def test_sample_analyses_exposes_method_and_checked_state(self):
        response = self.client.get("/api/v1/samples/SAMEA_TEST/analyses")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["checked"])
        self.assertEqual(body["items"][0]["pipeline_version"], "V6.0")
        self.assertEqual(body["items"][0]["accession"], "MGYA_TEST")

    def test_sample_taxonomy_retains_reference_and_count_semantics(self):
        response = self.client.get("/api/v1/samples/SAMEA_TEST/taxonomy?limit=10")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["items"][0]["reference_database"], "SILVA-SSU")
        self.assertEqual(body["items"][0]["reported_value"], 12.0)
        self.assertIn("not normalized", body["message"])

    def test_sample_list_enriches_study_title_and_biome_filter(self):
        response = self.client.get("/api/v1/samples?limit=10&biome=marine")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["study_title"], "Marine test study")

    def test_sample_functions_are_labeled_as_predictions(self):
        response=self.client.get("/api/v1/samples/SAMEA_TEST/functions")
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()["items"][0]["annotation_database"],"KEGG Orthology")
        self.assertIn("not evidence of expression",response.json()["message"])

    def test_search_returns_samples_without_coordinates(self):
        with duckdb.connect(str(api.DB)) as con:
            con.execute("INSERT INTO samples VALUES ('SAMEA_UNLOCATED','MGYS_TEST','Marine','root:Environmental:Aquatic:Marine',NULL,NULL,NULL,NULL,NULL,0,'https://example.invalid/unlocated','fixture')")
        response=self.client.get("/api/v1/search/samples?q=SAMEA_UNLOCATED")
        self.assertEqual(response.status_code,200)
        body=response.json()
        self.assertTrue(body["includes_samples_without_coordinates"])
        self.assertIsNone(body["items"][0]["latitude"])

    def test_timeline_respects_exact_habitat_and_accession_filters(self):
        marine=self.client.get("/api/v1/timeseries?habitat=Marine").json()["items"]
        soil=self.client.get("/api/v1/timeseries?habitat=Soil").json()["items"]
        search=self.client.get("/api/v1/timeseries?q=SAMEA_TEST").json()["items"]
        self.assertEqual(marine,[{"year":"2020","samples":1}])
        self.assertEqual(soil,[])
        self.assertEqual(search,[{"year":"2020","samples":1}])

    def test_alpha_diversity_is_scoped_to_analysis_database_and_rank(self):
        response=self.client.get("/api/v1/analyses/MGYA_TEST/alpha-diversity?reference_database=SILVA-SSU&rank=Genus")
        self.assertEqual(response.status_code,200)
        body=response.json()
        self.assertEqual(body["observed_richness"],1)
        self.assertEqual(body["shannon"],0)
        self.assertEqual(body["simpson"],0)
        self.assertIn("not the complete ecosystem",body["definition"])

    def test_community_comparison_requires_matching_profile_and_marker(self):
        with duckdb.connect(str(api.DB)) as con:
            con.execute("INSERT INTO samples VALUES ('SAMEA_TEST2','MGYS_TEST','Marine','root:Environmental:Aquatic:Marine',NULL,NULL,NULL,NULL,NULL,0,'https://example.invalid/sample2','fixture')")
            con.execute("INSERT INTO analyses VALUES ('MGYA_TEST2','ERR_TEST2','SAMEA_TEST2','MGYS_TEST','Amplicon','V6.0',NULL,'https://example.invalid/analysis2','{}',NULL)")
            con.execute("INSERT INTO analysis_downloads VALUES ('MGYA_TEST2','tsv','Statistics',NULL,'ERRTEST2_16S-V3-V4_asv_read_counts.tsv',NULL,NULL,'https://example.invalid/counts',NULL)")
            con.execute("INSERT INTO taxonomy_observations VALUES ('obs2','MGYA_TEST2','SAMEA_TEST2','ERR_TEST2','MGnify V6 amplicon taxonomy summary','SILVA-SSU','123','Pseudomonas','Genus','sk__Bacteria;p__Proteobacteria;g__Pseudomonas',8,'BIOM count','https://example.invalid/profile2','hash','0.2.0')")
            con.execute("INSERT INTO taxonomy_observations VALUES ('obs3','MGYA_TEST2','SAMEA_TEST2','ERR_TEST2','MGnify V6 amplicon taxonomy summary','SILVA-SSU','124','Bacillus','Genus','sk__Bacteria;p__Bacillota;g__Bacillus',2,'BIOM count','https://example.invalid/profile2','hash','0.2.0')")
            con.execute("INSERT INTO analysis_downloads VALUES ('MGYA_TEST','tsv','Statistics',NULL,'ERRTEST1_16S-V3-V4_asv_read_counts.tsv',NULL,NULL,'https://example.invalid/counts1',NULL)")
        response=self.client.get("/api/v1/community-comparison?sample_a=SAMEA_TEST&sample_b=SAMEA_TEST2&reference_database=SILVA-SSU&rank=Genus")
        self.assertEqual(response.status_code,200)
        body=response.json()
        self.assertEqual(body["marker_region"],"16S-V3-V4")
        self.assertEqual(body["shared_taxa"],1)
        self.assertAlmostEqual(body["bray_curtis_dissimilarity"],6/22)
        self.assertEqual(body["jaccard_similarity"],0.5)

    def test_bounding_box_can_cross_the_antimeridian(self):
        with duckdb.connect(str(api.DB)) as con:
            con.execute("INSERT INTO samples VALUES ('SAMEA_WEST','MGYS_TEST','Marine','root:Environmental:Aquatic:Marine',0,-175,NULL,NULL,NULL,0,'https://example.invalid/west','fixture')")
        response=self.client.get("/api/v1/samples?bbox=170,-10,-170,10")
        self.assertEqual(response.status_code,200)
        self.assertEqual([item["accession"] for item in response.json()["items"]],["SAMEA_WEST"])


if __name__ == "__main__":
    unittest.main()
