# Data terms

The repository software is MIT licensed. That licence does not cover the
published tennis facts or waive source-specific conditions.

Open Tennis Data v3 is a source-attributed research dataset and is not
represented as commercially reusable. Every release includes
`sources.parquet`, which records the applicable source URL, terms URL,
attribution, allowed uses, policy state, parser version, review date, and
content checksum.

Current source roles:

- Sackmann/Tennis Abstract: CC BY-NC-SA 4.0 research-only identity/result
  cross-checking. Attribution and ShareAlike apply; commercial use is not
  permitted.
- Wikimedia: applicable CC BY-SA terms and page-revision attribution.
- Tennis-Data.co.uk: public season files used under the source notice recorded
  in the registry. This repository does not claim a commercial redistribution
  grant.
- Community corrections: contributors dedicate the factual correction to CC0
  1.0.
- WTA and Tennis TV automation/publication: blocked by policy unless separate
  written permission is recorded.

An observation is excluded if its source is unregistered, policy-blocked, or
does not explicitly allow the research-release use. Public visibility, a free
account, or an accessible endpoint is not permission to automate or
redistribute.

This summary is not legal advice. Consumers are responsible for reviewing
`sources.parquet`,
[`src/open_tennis_data/sources.json`](src/open_tennis_data/sources.json), and the linked
terms before using or redistributing the data.
