# GMRS and Mode S radio listener

The default `gmrs` mode receives voice transmissions and can periodically
switch to 900 MHz meter decoding. Select `--mode adsb` to receive 1090 MHz
Mode S / ADS-B frames instead. One RTL-SDR can receive only one band at a time.

This receive-only service listens continuously to channels **1–7 and 15–22**
with one RTL-SDR. It records each channel independently, transcribes completed
recordings locally with whisper.cpp, and uploads an MP3 plus its transcript to
Logship. Channels 8–14 are 5 MHz away and require a second tuner for continuous
coverage; this version does not monitor them. The channel centers come from the
[FCC FRS/GMRS table](https://www.ecfr.gov/current/title-47/chapter-I/subchapter-D/part-95/subpart-B/section-95.563).

## How it works

`rtl_tcp` keeps the tuner open and supplies a 1.024 MS/s IQ stream centered at
462.450 MHz to the GMRS decoder. A shared
FFT and per-channel filters produce 15 narrowband FM audio streams. An adaptive
power squelch requires a 12 dB rise above the channel's noise floor for at least
0.12 seconds, and rejects rises shared by most channels. It starts each recording
with a 0.35 second lead-in and ends it after 0.7 seconds of silence. Completed
raw audio goes into a persistent spool. A
separate worker encodes 64 kb/s mono MP3, runs the English `base.en` Whisper
model, uploads the blob, and ingests a Logship row. Files remain in the spool
until Logship accepts the row. The capture path never waits for transcription or
the network.

With meter scanning enabled, GMRS runs for five minutes, then releases its stream
for up to 30 seconds while `rtlamr` connects to the same tuner and decodes ERT
SCM and SCM+ packets. The tuner is retuned through `rtl_tcp`; the application
and SDR server remain running. With a
configured endpoint ID, the scan returns to GMRS as soon as that meter is
decoded. GMRS transmissions during a meter window can be missed, and an active
recording is finalized at the switch. An OpenWay AMI7 may not broadcast SCM or
SCM+ while joined to its OpenWay network. `rtlamr` does not decode native
OpenWay network traffic, so reception must be verified locally.

Logship's blob endpoint serves downloads as attachments with
`application/octet-stream`. The MP3 can be played in a browser after download;
the Logship frontend does not provide an inline player through this API.

## Logship setup

Use an existing Logship account with a **nonzero UUID**. On every startup the
listener attempts to declare `channel` and `frequency_hz` as `Int64`,
`recording` as `BlobReference`, `started_at` and `ended_at` as `DateTime`,
`duration_seconds` as `Float64`, and `transcript` as `String` before starting
the radio. It uses `LOGSHIP_ADMIN_TOKEN` when provided, or
`LOGSHIP_TOKEN` otherwise. The token used for declaration needs account
table-management permission. With an inflow-only token, Logship returns 403;
the listener logs a warning and continues because the table may already have
been declared by an administrator. Other declaration errors stop startup.
Logship does not expose the table definition to an inflow-only token, so that
token cannot independently verify the column. A missing or mistyped column
will cause blob-bearing ingestion to fail and recordings to remain in the spool.
Logship cannot change an already inferred column's type. If this table already
has `started_at` or `ended_at` as `String`, the declaration returns HTTP 409;
existing rows need a separate migration or a new table before those column
names can become `DateTime`.
The equivalent manual request is:

```sh
curl -X PUT "$LOGSHIP_URL/accounts/$ACCOUNT_ID/tables/gmrs.transmissions" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"columns":[{"name":"channel","type":"Int64"},{"name":"frequency_hz","type":"Int64"},{"name":"recording","type":"BlobReference"},{"name":"started_at","type":"DateTime"},{"name":"ended_at","type":"DateTime"},{"name":"duration_seconds","type":"Float64"},{"name":"transcript","type":"String"}]}'
```

Create a personal access token with inflow scope for `gmrs.transmissions` and
use it as `LOGSHIP_TOKEN`. The blob API needs this scope to upload for that
table. Store both tokens in the service environment file if using a separate
management token. See the [Logship blob API](https://docs.logship.io/database/v0.0.1/blob-storage/)
and [table declaration guide](https://docs.logship.io/database/v0.0.1/tables/).

For ADS-B mode, the token also needs inflow scope for `adsb.messages`. On
startup, the application declares its `decoded` column as `Json`. ADS-B rows
contain each accepted raw Mode S frame, downlink format, aircraft address when
known, common decoded fields, and the complete decoder result. The decoder
cannot derive every field from every frame. Frames without a position, for
example, have no latitude or longitude.

Meter scanning also needs inflow scope for `meters.readings`. The app declares
the table on startup. Rows contain `meter_id`, `protocol`, `endpoint_type`, and
`consumption_raw`. This is the raw cumulative count; compare it with the meter
display before assuming its unit or applying a multiplier.

Each row contains `channel`, `frequency_hz`, `started_at`, `ended_at`,
`duration_seconds`, `transcript`, and the `recording` blob reference. The worker
uses stable blob and ingestion UUIDs across retries.

## NixOS installation

Add this repository as an input to your host flake and import its module:
Keep machine-specific paths, addresses, and Logship account settings in your
host configuration under `/etc/nixos/` rather than in this repository.

```nix
{
  inputs.radio-listener.url = "path:/path/to/radio-listener";

  outputs = { self, nixpkgs, radio-listener, ... }: {
    nixosConfigurations.your-host = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        radio-listener.nixosModules.default
        ({ pkgs, ... }: {
          services.radio-listener = {
            enable = true;
            mode = "gmrs"; # or "adsb"
            meter = {
              enable = true;
              intervalSeconds = 300.0;
              windowSeconds = 30.0;
              id = ""; # Discover candidate IDs; upload none
            };
            logshipUrl = "https://your-logship-backend";
            accountId = "your-nonzero-account-uuid";
            tokenFile = "/etc/radio-listener/token.env";
            # For an RTL-SDR Blog V4:
            # sdrPackage = pkgs.rtl-sdr-blog;
          };
        })
      ];
    };
  };
}
```

The module installs the SDR udev rules, adds the service user to `plugdev`,
blacklists the DVB driver that otherwise claims many RTL devices, and starts a
systemd service. Reboot after enabling the module so the driver and udev changes
take effect. If another SDR program needs the DVB driver, adjust that host-wide
setting before rebuilding.

For GMRS mode, the NixOS module fetches the checksum-pinned
[whisper.cpp `base.en` model](https://github.com/ggml-org/whisper.cpp/blob/master/models/README.md)
during the build and points the service at the Nix store copy. No manual model
installation is needed. Set `modelPath` to use a different local model. ADS-B
mode does not fetch the model.

Create `/etc/radio-listener/token.env` outside the Nix store with an inflow
token and optionally a separate table-management token:

```text
LOGSHIP_TOKEN=your-inflow-token
LOGSHIP_ADMIN_TOKEN=your-table-management-token
```

Make it readable by `radio-listener` and restrict access, for example
`root:radio-listener` with mode `0640`. Never place the token in a Nix option or
commit it to the repository. After rebuilding, check
`journalctl -u radio-listener -f` and `systemctl status radio-listener`.

The service stores pending recordings in `/var/lib/radio-listener/spool` and
stops starting new recordings when it reaches the default 5 GB limit. It logs
that condition. Completed uploads are removed locally. Adjust `gainDb`, `ppm`,
and `squelchDb` in the NixOS module options for your dongle and noise level.
`squelchDb` is the power margin above the channel's learned noise floor. The
first second after starting or reconnecting is used to establish that floor.
Watch `journalctl -u radio-listener -f` for candidate meter IDs, then set
`services.radio-listener.meter.id` to your meter's decimal endpoint ID and
rebuild. Confirm the ID by comparing readings with the physical display.
Nearby meters may also appear. With an empty ID, each scan uses the full window,
logs unique candidates, and uploads no readings.

## Manual run and tests

Copy `nix/manual.toml` to `~/.config/radio-listener/config.toml`, then set its
Logship URL and account ID. Put `LOGSHIP_TOKEN=...` in
`~/.config/radio-listener/token.env` with permissions `0600`. Run the receiver
in the foreground without installing the system service:

```sh
nix run .#listen
nix run .#listen -- --mode adsb
```

Run one of these commands at a time; press Ctrl+C to stop it. The `#listen` app
loads the config and token file from `~/.config/radio-listener/`. Override either
path with `RADIO_LISTENER_CONFIG` or `RADIO_LISTENER_TOKEN_FILE`. Store the Whisper
model at the path specified in the config for GMRS mode.
Logs show when a channel opens, the final recording duration, and the completed
transcript. For the system service, follow them with
`journalctl -u radio-listener -f`.
Only one process should use the RTL-SDR at a time; stop the system service first
if it has been activated. To validate the config without capturing radio, run
`nix run .#listen -- --check-config`.

ADS-B mode uses `rtl_sdr` at 1090 MHz, FlightAware's `dump1090-fa` to detect
valid Mode S frames, and `pyModeS` to decode them. It queues every frame in
`spool/adsb.sqlite` and uploads batches to Logship. The queue survives
restarts and has the same size limit as the GMRS spool. If Logship accepts a
batch but its response is lost, a retry may create duplicate rows. ADS-B mode
does not need the Whisper model, audio recording, or blob storage. The service
logs message counts and upload failures.

On a Linux machine with `rtl_sdr`, `ffmpeg`, `whisper-cli`, Python 3.11+, NumPy,
SciPy, and (for meter scanning) `rtlamr` installed:

```sh
python -m pip install .
export LOGSHIP_TOKEN='your-inflow-token'
radio-listener --config config.example.toml --check-config
radio-listener --config config.example.toml
```

Edit `config.example.toml` first. Its account ID and URL are placeholders.
`python -m unittest discover -s tests` runs the synthetic IQ, recording, and
Logship client tests. A live hardware check should confirm that two simultaneous
transmissions on different selected channels produce separate rows with usable
MP3 files.

For a receiver speed and channel-power check, capture IQ with `rtl_sdr -d 0 -f
462450000 -s 1024000 -g 30 -n 1024000 sample.iq`, then run
`radio-listener-probe sample.iq`. The reported processing time should be less
than the capture duration on the machine that will run the service.
