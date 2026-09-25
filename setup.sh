#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# setup.sh - provision the remote ComfyUI instance (delphi) so the workflows in
# workflows/ can run: Z-Image-Turbo text-to-image + TRELLIS.2 image-to-3D, and
# Stable Audio Open 1.0 text-to-audio (sound effects).
#
# The host runs the AMD "ryai-comfyui" image in a systemd-managed, *ephemeral*
# podman container (comfyui@<port>.service).  The container is recreated on every
# restart, so everything we install must live in bind mounts (the ComfyUI base
# dir) or in a locally committed image.  The script is safe to re-run.
#
# Usage:
#   ./setup.sh
#   COMFY_REMOTE_HOST=host COMFY_REMOTE_PORT=8188 COMFY_REMOTE_USER=user ./setup.sh
#   GFX=gfx1151 ./setup.sh
# ---------------------------------------------------------------------------
set -euo pipefail

# Remote ComfyUI endpoint (all overridable via environment).
COMFY_REMOTE_HOST="${COMFY_REMOTE_HOST:-delphi}"
COMFY_REMOTE_PORT="${COMFY_REMOTE_PORT:-8188}"
COMFY_REMOTE_USER="${COMFY_REMOTE_USER:-febret}"

DELPHI="${DELPHI:-${COMFY_REMOTE_USER}@${COMFY_REMOTE_HOST}}"
HOST="${HOST:-$COMFY_REMOTE_HOST}"
PORT="${PORT:-$COMFY_REMOTE_PORT}"
BASE="${BASE:-/home/${COMFY_REMOTE_USER}/.local/share/ComfyUI}"
CODE="$BASE/code"
CUSTOM_NODES="$BASE/custom_nodes"
MODELS="$BASE/models"
UNIT="comfyui@${PORT}"
UNIT_DIR=".config/containers/systemd/comfyui@.container.d"
COMFY_VER="${COMFY_VER:-v0.37.0}"
BASE_IMAGE="oci-registry.ryai.dev/ryai-comfyui:latest"
IMAGE="oci-registry.ryai.dev/ryai-comfyui:trellis2"
HF="https://huggingface.co"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

say()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }
rc()   { ssh "$DELPHI" "$@"; }

# ---------------------------------------------------------------------------
# helper files (written locally with quoted heredocs, then copied to delphi)
# ---------------------------------------------------------------------------
cat > "$TMP/patch_server.py" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
if 'from systemd.daemon import' in s:
    print('server.py already patched'); sys.exit(0)
s = s.replace('from io import BytesIO\n',
              'from io import BytesIO\nfrom systemd.daemon import listen_fds, is_socket_inet\n', 1)
start = '\n        for addr in addresses:\n'
marker = 'logging.info("To see the GUI go to:'
i = s.index(start)
j = s.index(marker, i)
j = s.index('\n', j) + 1
resolved = '''
        sd_sa_fds = listen_fds()
        if sd_sa_fds:
            logging.info(f"Setting up socket activation for {sd_sa_fds}\\n")
            for fd in sd_sa_fds:
                if not is_socket_inet(fd):
                    logging.warning("Socket is not inet\\n")
                inet_sock = socket.socket(fileno=fd)
                site = web.SockSite(runner, inet_sock, ssl_context=ssl_ctx)
                try:
                    await site.start()
                except OSError as e:
                    if e.errno == errno.EADDRINUSE:
                        raise SystemExit(1)
                    raise
                (address, port, *_) = inet_sock.getsockname()
                if not hasattr(self, 'address'):
                    self.address = address
                    self.port = port
                address_print = "[{}]".format(address) if ':' in address else address
                if verbose:
                    logging.info("To see the GUI go to: {}://{}:{}".format(scheme, address_print, port))
        else:
            for addr in addresses:
                address, port = addr[0], addr[1]
                site = web.TCPSite(runner, address, port, ssl_context=ssl_ctx)
                try:
                    await site.start()
                except OSError as e:
                    if e.errno == errno.EADDRINUSE:
                        raise SystemExit(1)
                    raise
                if not hasattr(self, 'address'):
                    self.address = address
                    self.port = port
                address_print = "[{}]".format(address) if ':' in address else address
                if verbose:
                    logging.info("To see the GUI go to: {}://{}:{}".format(scheme, address_print, port))
'''
s = s[:i] + resolved + s[j:]
open(p, 'w', encoding='utf-8').write(s)
print('patched server.py')
PY

cat > "$TMP/early_gl.py" <<'PY'
import os as _os
_os.environ.setdefault("EGL_PLATFORM", "surfaceless")
try:
    import nvdiffrast.torch as _early_dr
    _EARLY_GLCTX = _early_dr.RasterizeGLContext()
    print("[EARLY WARMUP] GL context created successfully", flush=True)
except Exception as _e:
    print(f"[EARLY WARMUP] GL context failed: {_e}", flush=True)
PY

cat > "$TMP/prepend_early_gl.py" <<'PY'
import sys
main_p, hdr_p = sys.argv[1], sys.argv[2]
s = open(main_p, encoding='utf-8').read()
if 'EARLY WARMUP' in s:
    print('main.py already warmed'); sys.exit(0)
hdr = open(hdr_p, encoding='utf-8').read()
open(main_p, 'w', encoding='utf-8').write(hdr + '\n' + s)
print('main.py warmed')
PY

# ---------------------------------------------------------------------------
say "Checking access to $DELPHI"
rc "echo ok >/dev/null"
GFX="${GFX:-$(rc "rocminfo 2>/dev/null | grep -o 'gfx[0-9]*' | head -1" || true)}"
GFX="${GFX:-gfx1151}"
info "GPU arch: $GFX"

# ---------------------------------------------------------------------------
say "1/7  ComfyUI code tree ($COMFY_VER) + ryai patches"
rc "if [ ! -d '$CODE/.git' ]; then git clone --branch $COMFY_VER --depth 1 https://github.com/Comfy-Org/ComfyUI.git '$CODE'; fi"
rc "cd '$CODE' && for d in custom_nodes models; do if [ ! -L \$d ]; then rm -rf \$d; ln -sfn '$BASE/'\$d \$d; fi; done"
scp -q "$TMP/patch_server.py" "$DELPHI:/tmp/patch_server.py"
scp -q "$TMP/early_gl.py" "$TMP/prepend_early_gl.py" "$DELPHI:/tmp/"
rc "python3 /tmp/patch_server.py '$CODE/server.py' && python3 -m py_compile '$CODE/server.py'"
rc "python3 /tmp/prepend_early_gl.py '$CODE/main.py' /tmp/early_gl.py && python3 -m py_compile '$CODE/main.py'"

# ---------------------------------------------------------------------------
say "2/7  runtime image with GL/GUI libs + build toolchain ($IMAGE)"
if rc "podman image exists '$IMAGE'"; then
    info "image already present"
else
    info "building local image (apt layer + commit)..."
    rc "podman rm -f cfy-build 2>/dev/null || true; \
        podman run -d --name cfy-build --entrypoint sleep '$BASE_IMAGE' infinity >/dev/null && \
        podman exec cfy-build bash -c 'apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
            build-essential python3-dev curl libgl1 libegl1 libglx-mesa0 libglu1-mesa libglib2.0-0 libgomp1 \
            libxext6 libxrender1 libsm6 libice6 libxi6 libxfixes3 libxxf86vm1 libxrandr2 libxcursor1 \
            libusb-1.0-0 libarchive13 libtbb12 libcairo2 libfontconfig1 libopenblas0' && \
        podman stop cfy-build >/dev/null && podman commit cfy-build '$IMAGE' >/dev/null && podman rm cfy-build >/dev/null"
fi

say "2b/7  systemd drop-in"
rc "mkdir -p ~/$UNIT_DIR && cat > ~/$UNIT_DIR/override.conf <<'EOF'
[Container]
Image=$IMAGE
Entrypoint=/opt/comfyui/comfyui.sh
Volume=$CODE:/opt/comfyui/ComfyUI:Z
Environment=TORCH_BLAS_PREFER_HIPBLASLT=0
Environment=TRITON_CACHE_DIR=/opt/comfyui/base/.triton
EOF
systemctl --user daemon-reload"

# ---------------------------------------------------------------------------
say "3/7  custom nodes"
git clone --depth 1 https://github.com/toastmanAu/trellis-2-rocm-comfyui.git "$TMP/patches" >/dev/null 2>&1 || true
rc "mkdir -p '$CUSTOM_NODES'"
rc "[ -d '$CUSTOM_NODES/ComfyUI-GGUF/.git' ] || git clone --depth 1 https://github.com/city96/ComfyUI-GGUF.git '$CUSTOM_NODES/ComfyUI-GGUF'"
rc "[ -d '$CUSTOM_NODES/ComfyUI-Trellis2-GGUF/.git' ] || git clone https://github.com/Aero-Ex/ComfyUI-Trellis2-GGUF.git '$CUSTOM_NODES/ComfyUI-Trellis2-GGUF'"
if [ -d "$TMP/patches/patches" ]; then
    scp -q "$TMP/patches/patches/05-model-manager-bf16.patch" "$TMP/patches/patches/06-postprocess-infinite-vertices.patch" "$DELPHI:/tmp/"
    rc "cd '$CUSTOM_NODES/ComfyUI-Trellis2-GGUF' && for p in 05-model-manager-bf16 06-postprocess-infinite-vertices; do
          if git apply --check /tmp/\$p.patch 2>/dev/null; then git apply /tmp/\$p.patch && echo \"applied \$p\"; else echo \"\$p already applied\"; fi
        done"
fi

# ---------------------------------------------------------------------------
say "3b/7  TTS custom node (Chatterbox, prosody-capable)"
rc "[ -d '$CUSTOM_NODES/ComfyUI_Fill-ChatterBox/.git' ] || git clone --depth 1 https://github.com/filliptm/ComfyUI_Fill-ChatterBox.git '$CUSTOM_NODES/ComfyUI_Fill-ChatterBox'"
have_cb=$(rc "ls -d '$BASE/venv'/lib/python*/site-packages/librosa '$BASE/venv'/lib/python*/site-packages/s3tokenizer 2>/dev/null | wc -l" || echo 0)
if [ "${have_cb:-0}" -ge 2 ]; then
    info "Chatterbox python deps already installed"
else
    info "installing Chatterbox python deps (librosa, s3tokenizer, ...)"
    rc "podman rm -f cb-build 2>/dev/null || true; podman run -d --name cb-build --entrypoint sleep -v '$BASE:/opt/comfyui/base:Z' '$IMAGE' infinity >/dev/null"
    rc "podman exec cb-build bash -c 'source /opt/comfyui/base/venv/bin/activate && pip install --no-cache-dir -r /opt/comfyui/base/custom_nodes/ComfyUI_Fill-ChatterBox/requirements.txt 2>&1 | tail -2'"
    rc "podman rm -f cb-build >/dev/null 2>&1 || true"
fi

# ---------------------------------------------------------------------------
say "4/7  ROCm HIP extensions (cumesh, flex_gemm, nvdiffrast, o_voxel)"
have_ext=$(rc "ls -d '$BASE/venv'/lib/python*/site-packages/cumesh '$BASE/venv'/lib/python*/site-packages/flex_gemm '$BASE/venv'/lib/python*/site-packages/nvdiffrast '$BASE/venv'/lib/python*/site-packages/o_voxel 2>/dev/null | wc -l" || echo 0)
if [ "${have_ext:-0}" -ge 4 ]; then
    info "extensions already installed"
else
    info "running the community ROCm installer in a build container (10-30 min)..."
    rc "systemctl --user stop ${UNIT}.service 2>/dev/null || true"
    rc "podman rm -f trellis-build 2>/dev/null || true; \
        podman run -d --name trellis-build --entrypoint sleep --device /dev/kfd --device /dev/dri/renderD128 \
          -v '$BASE:/opt/comfyui/base:Z' -v '$CODE:/opt/comfyui/ComfyUI:Z' -v /var/cache/models:/var/cache/models:Z \
          '$IMAGE' infinity >/dev/null"
    rc "podman exec trellis-build bash -c 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl cmake ninja-build pkg-config 2>&1 | tail -1'"
    rc "rm -rf /tmp/egore && git clone --depth 1 https://github.com/egore/comfyui-trellis2-gguf-rocm.git /tmp/egore"
    rc "podman cp /tmp/egore/install-trellis2-gguf-rocm.sh trellis-build:/opt/comfyui/install.sh"
    rc "podman exec trellis-build bash -c 'ln -sfn /opt/comfyui/base/venv /opt/comfyui/comfy-env; \
          if [ ! -L /opt/comfyui/ComfyUI/custom_nodes ]; then rm -rf /opt/comfyui/ComfyUI/custom_nodes; ln -sfn /opt/comfyui/base/custom_nodes /opt/comfyui/ComfyUI/custom_nodes; fi; \
          if [ ! -L /opt/comfyui/ComfyUI/models ]; then rm -rf /opt/comfyui/ComfyUI/models; ln -sfn /opt/comfyui/base/models /opt/comfyui/ComfyUI/models; fi; \
          sed -i \"s/gfx1102/$GFX/g\" /opt/comfyui/install.sh; \
          cd /opt/comfyui && bash install.sh > /opt/comfyui/base/install-trellis.log 2>&1; echo exit=\$?'"
    rc "podman exec trellis-build bash -c 'source /opt/comfyui/base/venv/bin/activate && \
        pip install --no-cache-dir -r /opt/comfyui/base/custom_nodes/ComfyUI-Trellis2-GGUF/requirements.txt \"huggingface-hub>=1.3,<2\" \"tokenizers>=0.22,<=0.23.0\" 2>&1 | tail -2'"
    rc "podman rm -f trellis-build >/dev/null 2>&1 || true"
fi

# ---------------------------------------------------------------------------
say "5/7  models"
dl() { rc "if [ ! -s '$MODELS/$2' ]; then mkdir -p \"\$(dirname '$MODELS/$2')\"; curl -fL -C - --retry 10 --retry-all-errors -o '$MODELS/$2' '$1'; fi"; }
dl "$HF/Comfy-Org/z_image_turbo/resolve/main/split_files/diffusion_models/z_image_turbo_bf16.safetensors" "diffusion_models/z_image_turbo_bf16.safetensors"
dl "$HF/Comfy-Org/z_image_turbo/resolve/main/split_files/text_encoders/qwen_3_4b.safetensors" "text_encoders/qwen_3_4b.safetensors"
dl "$HF/Comfy-Org/z_image_turbo/resolve/main/split_files/vae/ae.safetensors" "vae/ae.safetensors"
dl "$HF/Comfy-Org/BiRefNet/resolve/main/background_removal/birefnet.safetensors" "background_removal/birefnet.safetensors"
dl "$HF/PIA-SPACE-LAB/dinov3-vitl16-pretrain-lvd1689m/resolve/main/model.safetensors" "facebook/dinov3-vitl16-pretrain-lvd1689m/model.safetensors"
dl "$HF/PIA-SPACE-LAB/dinov3-vitl16-pretrain-lvd1689m/resolve/main/config.json" "facebook/dinov3-vitl16-pretrain-lvd1689m/config.json"
dl "$HF/PIA-SPACE-LAB/dinov3-vitl16-pretrain-lvd1689m/resolve/main/preprocessor_config.json" "facebook/dinov3-vitl16-pretrain-lvd1689m/preprocessor_config.json"
# Stable Audio Open 1.0 (text -> sound effects). Core ComfyUI nodes only; audio
# save/load uses the bundled PyAV (av), so no extra system packages are needed.
# stabilityai/stable-audio-open-1.0 is gated; this Comfy-Org repack is public.
dl "$HF/Comfy-Org/stable-audio-open-1.0_repackaged/resolve/main/stable-audio-open-1.0.safetensors" "checkpoints/stable-audio-open-1.0.safetensors"
dl "$HF/ComfyUI-Wiki/t5-base/resolve/main/t5-base.safetensors" "text_encoders/t5-base.safetensors"
# Stable Audio 3 Small-SFX (alternative SFX model; core nodes only, ~3.5 GB).
dl "$HF/Comfy-Org/stable-audio-3/resolve/main/checkpoints/stable_audio_3_small_sfx_base.safetensors" "checkpoints/stable_audio_3_small_sfx_base.safetensors"
dl "$HF/Comfy-Org/stable-audio-3/resolve/main/text_encoders/t5gemma_b_b_ul2.safetensors" "text_encoders/t5gemma_b_b_ul2.safetensors"
# Chatterbox TTS (prosody). The nodes auto-download to models/chatterbox/ on
# first use; pre-fetching here keeps generation from stalling on big downloads.
for f in ve.safetensors t3_cfg.safetensors s3gen.safetensors tokenizer.json conds.pt; do
    dl "$HF/ResembleAI/chatterbox/resolve/main/$f" "chatterbox/chatterbox/$f"
done
for f in ve.safetensors t3_turbo_v1.safetensors s3gen_meanflow.safetensors tokenizer_config.json special_tokens_map.json vocab.json merges.txt added_tokens.json conds.pt; do
    dl "$HF/ResembleAI/chatterbox-turbo/resolve/main/$f" "chatterbox/chatterbox_turbo/$f"
done
info "TRELLIS.2 GGUF Q8_0 weights auto-download to models/Trellis2/ on the first run (~4.5 GB)."

# ---------------------------------------------------------------------------
say "6/7  restarting ComfyUI"
rc "systemctl --user restart ${UNIT}.service"
info "waiting for the API..."
for _ in $(seq 1 36); do
    sleep 5
    if curl -s "http://$HOST:$PORT/system_stats" >/dev/null 2>&1; then break; fi
done

# ---------------------------------------------------------------------------
say "7/7  verification"
curl -s "http://$HOST:$PORT/object_info" | python3 -c "
import json,sys
d=json.load(sys.stdin)
need=['Trellis2LoadModel_GGUF','Trellis2MeshWithVoxelAdvancedGenerator_GGUF','Trellis2MeshTexturing_GGUF','UNETLoader','CLIPLoader','RemoveBackground','EmptyLatentAudio','VAEDecodeAudio','SaveAudioAdvanced','ConditioningStableAudio','TrimAudioDuration','FL_ChatterboxTTS']
missing=[n for n in need if n not in d]
print('OK - required nodes present' if not missing else 'MISSING: '+str(missing))
" || echo "   (could not query object_info; check host/port)"

echo
echo "Done. Try:"
echo "  python generate_asset.py \"low poly game model of a treasure chest, 16 color palette, isometric view, neutral background, game asset\" --render"
echo "  python generate_sound.py \"kitty meowing, close-up, foley sound effect\" --name cat_meow --duration 5 --wav"
echo "  python generate_speech.py \"The gate is locked. Find another way around.\" --name guard --exaggeration 0.5"
