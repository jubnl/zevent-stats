Sources of the synthetic dumps in this directory (see zevent_tracker/external.py).

- The dumps themselves come from zevent.shellgratuit.com's public Grafana (Prometheus-style store scraping
  the official API every 30 s), resampled to one-minute ticks.
- louis-julien/*.json: per-channel donation counters from zevent-stats.louis-julien.dev
  (/streamer/<login>/__data.json?r=all, official API sampled every 10 s, published at 30-minute steps).
  They replace the shellgratuit counters of the two organisation channels, "zevent" and "zeventplays",
  whose counters that store reports swapped or zeroed. Values are held between the 30-minute points.
