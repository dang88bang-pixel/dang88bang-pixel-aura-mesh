# App assets

## `visualizer/`

`MapFragment` loads `file:///android_asset/visualizer/index.html`. That bundle
is **built from `web-visualizer/`**, not maintained separately — shipping two
renderers would let the semantic colour code drift between the handheld and the
command-post view, which is precisely the confusion the shared palette exists
to prevent.

Populate it before packaging a release:

```bash
./tools/bundle-visualizer.sh
```

The directory is intentionally left empty in git (see `.gitignore`); the
fragment degrades to an explanatory placeholder when the bundle is absent, so a
debug build still installs and runs.

## Optional GGUF model

`LLMService` looks for a model in this order:

1. `filesDir/<name>.gguf` (already extracted)
2. `getExternalFilesDir()/<name>.gguf` (side-loaded — the normal route)
3. `assets/<name>.gguf` (bundled)

Do **not** commit a GGUF here. A 1–2.4 GB asset blows past the 200 MB Play
limit and makes every CI checkout slow. Side-load it at provisioning:

```bash
adb push qwen2.5-1.5b-instruct-q4_k_m.gguf \
  /sdcard/Android/data/com.aura.agent/files/
```
