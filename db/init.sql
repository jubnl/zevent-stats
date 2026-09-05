CREATE TABLE snapshot (
  ts               timestamptz PRIMARY KEY,
  donation_total   numeric(14,2) NOT NULL,
  viewers_total    integer NOT NULL,
  streamers_total  integer NOT NULL,
  streamers_online integer NOT NULL
);

CREATE TABLE streamer (
  twitch_id    text PRIMARY KEY,
  login        text NOT NULL,
  display      text NOT NULL,
  profile_url  text,
  donation_url text,
  location     text,          -- 'LAN' (on site) or 'Online' (streaming from home)
  first_seen   timestamptz NOT NULL,
  last_seen    timestamptz NOT NULL
);

CREATE TABLE streamer_sample (
  ts             timestamptz NOT NULL,
  twitch_id      text NOT NULL REFERENCES streamer(twitch_id),
  online         boolean NOT NULL,
  game           text,
  viewers        integer NOT NULL,
  donation_total numeric(12,2) NOT NULL,
  PRIMARY KEY (twitch_id, ts)
);
CREATE INDEX streamer_sample_ts_idx ON streamer_sample (ts);

CREATE ROLE grafana LOGIN PASSWORD 'grafana';
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana;
