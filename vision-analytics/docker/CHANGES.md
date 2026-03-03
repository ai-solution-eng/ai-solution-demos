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
- Added shared-pv: Dynamically mirrors existing 'ezpresto-shared-pv' using Helm lookup (no fallback) to enable read-only access to shared data
- Added shared-pvc: Binds to the mirrored shared PV
- Updated deployment: Mounts shared volume at /mnt/shared (read-only)
- Added datasources mount: mounts /mnt/datasources from datasources (currently not used - file explorer traverse might take too long)
- Updated system prompt: added system prompt for image LLM calls.

v0.0.3
- Added build.sh: script to build and push the docker image
- Added RBAC to allow app to read inference service endpoints
- Added dropdown listing of available inference services
- Added default mount point /mnt as sandbox for the app so user cannot browse the whole filesystem
- Added root folder selection in settings page, so video and image browsers default to that path
- Added "Ignore TLS verification" checkbox in settings page, so user can ignore TLS verification for inference services (for self-signed certificates)
- Added support for .mov files (will be automatically copied and converted in a read-write temporary directory) - loading mov files takes longer!
- Glob settings to file explorer components to filter jpg/png files (for images) and mp4/mov files (for videos)
- Removed favicon.ico
- Removed datasources mount

v0.0.4
- Added local RTSP server functionality for video analysis
- Added video player to display the RTSP stream
- Added error handling for video processing
- Added configuration persistence
- Moved container image to pcai-demos repository