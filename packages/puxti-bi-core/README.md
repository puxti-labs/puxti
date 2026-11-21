# puxti-bi-core

The spec schema for BI assets as code, used by [Puxti](https://github.com/puxti-labs/puxti).

Datasets, charts and dashboards are short YAML files in git, one asset per file. This package
parses and validates them and publishes their JSON Schema. It depends on no Puxti package;
Puxti resolves the dbt names the specs refer to.

The schema is `puxti/v1alpha1` and may still change in breaking ways. See
[docs/bi-specs.md](https://github.com/puxti-labs/puxti/blob/main/docs/bi-specs.md) for the format.
