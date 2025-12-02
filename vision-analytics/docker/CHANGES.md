v0.0.1
- Initial release

v0.0.2
- Added CHANGES.md
- Added requirements.txt: streamlined docker image with locked dependencies
- Added config.json: added functionality for persistent API settings, settings are now saved to disk for all users, reading from environment at first run
- Added assets: added assets directory for static examples with prompts
- Getting started instructions collapsed
- Added Settings tab: API settings form moved to its own tab
- Added File Explorer: added file explorer for easy access to files on the shared data sources in PCAI
- Removed image preview: selected image is now shown in image upload component
- Set image and video height values to 400px
- Added kyverno-policy: Added label assignment policy for the app, hpe-ezua/type: vendor-service
- Added datasources mount: mounts /mnt/datasources from datasources (currently not used - file explorer traverse might take too long)
- Added shared-pv: Dynamically mirrors existing 'ezpresto-shared-pv' using Helm lookup (no fallback) to enable read-only access to shared data
- Added shared-pvc: Binds to the mirrored shared PV
- Updated deployment: Mounts shared volume at /mnt/shared (read-only)
