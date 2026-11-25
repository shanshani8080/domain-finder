# domain-finder

A daily pipeline that watches TLD zone files for domains leaving the zone,
screens the names against free quality metrics and Wayback history, tracks
them through the deletion cycle, and emails an alert the day a vetted name
actually becomes registerable at normal fixed registration price.

This public repo holds only the engine. All configuration, results, logs and
state live in a separate private data repository, so nothing about what the
pipeline finds is visible here. Run logs print counts only.

## How it works

1. Zone diff: yesterday's zone vs today's (ICANN CZDS). Names that left the
   zone are expiring; this happens 35 to 75 days before the actual drop.
2. Name filter: length, characters, lexicon screens.
3. Staged screening inside free API quotas: Open PageRank, then Wayback CDX
   history vetting, then Ahrefs DR for the shortlist.
4. Watch ledger: survivors wait out redemption; RDAP confirms the moment a
   name is actually deleted and unregistered.
5. Alert: email with score and evidence links, and a dashboard feed written
   to the private data repo.

## Setup

Run `update.bat` from the release folder on Windows. It creates the private
data repo, installs the deploy key, publishes this code and starts the first
run. Secrets (CZDS, email, metric APIs) are set once in the repo settings.
