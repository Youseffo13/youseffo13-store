## Youseffo13's Community App Store

Just some apps I like to use and want to integrate into Umbrel.

## Automatic app updates

Umbrel shows an update for an installed app when the `version` in that app's
`umbrel-app.yml` differs from the installed version, and it pulls new container
code when the image in `docker-compose.yml` changes. Both files therefore have
to be updated together.

This repository updates them automatically with
[`.github/workflows/update-apps.yml`](.github/workflows/update-apps.yml), which
runs `scripts/update_apps.py` every day (and on demand from the Actions tab).

The script:

1. finds every top-level app directory containing `umbrel-app.yml`,
2. reads the image used by its compose service,
3. asks Docker Hub / GHCR for the published tags,
4. picks the newest stable numeric tag (skipping `latest`, `nightly`, `dev`,
   `beta`, `rc`, …),
5. writes the new tag to `docker-compose.yml` and the new version to
   `umbrel-app.yml`, then the workflow commits and pushes the change.

You can run it locally too:

```sh
python3 scripts/update_apps.py --dry-run   # show what would change
python3 scripts/update_apps.py             # apply the changes
```

Per-app overrides go in [`.github/apps.json`](.github/apps.json):

```json
{
  "my-app": {
    "service": "server",
    "image": "owner/image",
    "allowMajor": false
  }
}
```

- `service` — compose service that runs the app (default: first service with an
  `image`).
- `image` — image reference to track when the compose file contains several
  images or the default detection picks the wrong one. It must point to the same
  repository used in `docker-compose.yml`.
- `allowMajor` — set to `false` to stay on the current major version (for
  example, keep `1.x` instead of jumping to `2.x`).

Note: the top-level `version:` key in a Compose file is obsolete in Compose v2
and is not used by Umbrel; it has been removed from the compose files here.
