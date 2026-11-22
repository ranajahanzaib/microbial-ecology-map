import unittest

from backend.ingest import attrs, items, parse_assembly_function_tsv, parse_biom_taxonomy_tsv, record_accession, value


class ParsingTests(unittest.TestCase):
    def test_flat_v2_records(self):
        record = {"accession": "SAMEA1", "latitude": "42.1"}
        self.assertEqual(record_accession(record), "SAMEA1")
        self.assertEqual(attrs(record)["latitude"], "42.1")

    def test_json_api_records_and_lists(self):
        record = {"id": "SAMEA2", "attributes": {"biome": "Environmental"}}
        self.assertEqual(record_accession(record), "SAMEA2")
        self.assertEqual(value(attrs(record), "biome"), "Environmental")
        self.assertEqual(items({"count": 1, "items": [record]}), [record])

    def test_missing_optional_values(self):
        self.assertIsNone(value({}, "latitude", "lat"))
        self.assertEqual(items({"items": None}), [])

    def test_v6_biom_taxonomy_summary_preserves_counts_and_unclassified(self):
        content = "# Constructed from biom file\n# OTU ID\tSILVA-SSU\ttaxonomy\ttaxid\n10\t12.0\tsk__Bacteria;k__;p__Proteobacteria;g__Pseudomonas\t123\n11\t2.0\t\t\n"
        rows = parse_biom_taxonomy_tsv(content)
        self.assertEqual(rows[0]["reference_database"], "SILVA-SSU")
        self.assertEqual(rows[0]["scientific_name"], "Pseudomonas")
        self.assertEqual(rows[0]["taxonomic_rank"], "Genus")
        self.assertEqual(rows[0]["reported_value"], 12.0)
        self.assertIn("not normalized abundance", rows[0]["value_semantics"])
        self.assertEqual(rows[1]["scientific_name"], "Unclassified")

    def test_taxonomy_parser_rejects_unverified_formats(self):
        with self.assertRaises(ValueError):
            parse_biom_taxonomy_tsv("ASV\tDomain\tSpecies\nseq_1\tBacteria\tNA\n")

    def test_v6_assembly_pfam_and_kegg_summaries_are_distinct(self):
        pfam=parse_assembly_function_tsv("pfam\tdescription\tcount\nPF00005\tABC transporter\t2129\n", "ERZ1_pfam_summary.tsv.gz")
        self.assertEqual(pfam[0]["annotation_database"], "Pfam")
        self.assertEqual(pfam[0]["function_id"], "PF00005")
        self.assertEqual(pfam[0]["reported_value"], 2129)
        module=parse_assembly_function_tsv("module_accession\tcompleteness\tpathway_name\tpathway_class\tmatching_ko\tmissing_ko\nM00004\t100.0\tPentose phosphate pathway\tCarbohydrate metabolism\tK00033\t\n", "ERZ1_kegg_modules_summary.tsv.gz")
        self.assertEqual(module[0]["annotation_database"], "KEGG module")
        self.assertIn("completeness percentage", module[0]["value_semantics"])
        self.assertEqual(module[0]["functional_category"], "Carbohydrate metabolism")

    def test_function_parser_rejects_unverified_filename_format(self):
        with self.assertRaises(ValueError):
            parse_assembly_function_tsv("id\tvalue\na\t1\n", "ERZ1_random.tsv.gz")


if __name__ == "__main__":
    unittest.main()
