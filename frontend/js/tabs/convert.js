/* Soundloom convert tab — in-browser FFmpeg.wasm conversion (from Music Converter). */
import { showToast, esc } from '../core.js';

export const Convert = {
  ffmpeg: null,
  files: [],
  running: false,
  cancelled: false,
  CDN: 'https://cdn.jsdelivr.net/npm/@ffmpeg/core@0.12.6/dist/esm',
  PKG: 'https://cdn.jsdelivr.net/npm/@ffmpeg/ffmpeg@0.12.10/+esm',
  UTIL: 'https://cdn.jsdelivr.net/npm/@ffmpeg/util@0.12.1/+esm',

  init() {
    const input = document.getElementById('convert-input');
    if (!input || input._wired) return;
    input._wired = true;
    input.addEventListener('change', (e) => { this.add([...e.target.files]); input.value = ''; });

    const drop = document.getElementById('convert-drop');
    ['dragenter', 'dragover'].forEach(ev =>
      drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('dragover'); }));
    ['dragleave', 'drop'].forEach(ev =>
      drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('dragover'); }));
    drop.addEventListener('drop', (e) => { if (e.dataTransfer) this.add([...e.dataTransfer.files]); });
  },

  add(list) {
    for (const f of list) {
      if (!/^audio\//.test(f.type) && !/\.(mp3|flac|wav|ogg|m4a|aac|opus|wma|webm)$/i.test(f.name)) continue;
      this.files.push({ file: f, state: 'queued', progress: 0, url: null });
    }
    this.render();
  },

  async ensureFFmpeg(onProgress) {
    if (this.ffmpeg) return this.ffmpeg;
    onProgress('Loading FFmpeg engine (first run downloads ~30 MB)…');
    const { FFmpeg } = await import(this.PKG);
    const { toBlobURL } = await import(this.UTIL);
    const ff = new FFmpeg();
    ff.on('progress', ({ progress }) => onProgress(`Converting… ${Math.round(progress * 100)}%`, progress));
    // FFmpeg.wasm defaults to constructing its worker straight from the CDN
    // URL, which the browser refuses cross-origin. Hand it a same-origin blob
    // that re-imports that script.
    const workerSource = `import '${this.PKG.replace('/+esm', '/dist/esm/worker.js')}';`;
    const classWorkerURL = URL.createObjectURL(
      new Blob([workerSource], { type: 'text/javascript' })
    );
    try {
      await ff.load({
        coreURL: await toBlobURL(`${this.CDN}/ffmpeg-core.js`, 'text/javascript'),
        wasmURL: await toBlobURL(`${this.CDN}/ffmpeg-core.wasm`, 'application/wasm'),
        classWorkerURL,
      });
    } catch (e) {
      URL.revokeObjectURL(classWorkerURL);
      throw e;
    }
    this.ffmpeg = ff;
    return ff;
  },

  codecArgs(format, inputName, outputName) {
    const args = ['-i', inputName, '-vn'];
    switch (format) {
      case 'mp3': args.push('-c:a', 'libmp3lame', '-b:a', '192k'); break;
      case 'wav': args.push('-c:a', 'pcm_s16le'); break;
      case 'flac': args.push('-c:a', 'flac'); break;
      case 'ogg': args.push('-c:a', 'libvorbis', '-q:a', '5'); break;
      case 'm4a': args.push('-c:a', 'aac', '-b:a', '192k'); break;
      default: break;
    }
    // ffmpeg needs the output path as the final argument.
    args.push(outputName);
    return args;
  },

  async start() {
    if (this.running || !this.files.length) return;
    this.running = true;
    this.cancelled = false;
    const format = document.getElementById('convert-format').value;
    const status = document.getElementById('convert-status');
    document.getElementById('convert-start').disabled = true;
    document.getElementById('convert-cancel').style.display = '';

    try {
      const { fetchFile } = await import(this.UTIL);
      const ff = await this.ensureFFmpeg((msg, p) => {
        status.textContent = msg;
        if (p != null) {
          const cur = this.files.find(f => f.state === 'converting');
          if (cur) { cur.progress = p; this.render(); }
        }
      });

      for (let i = 0; i < this.files.length; i++) {
        if (this.cancelled) break;
        const item = this.files[i];
        item.state = 'converting';
        item.progress = 0;
        this.render();

        const base = item.file.name.replace(/\.[^.]+$/, '');
        const ext = (item.file.name.match(/\.([^.]+)$/) || [, 'bin'])[1].toLowerCase();
        const stamp = `${Date.now()}_${i}`;
        const inputName = `in_${stamp}.${ext}`;
        const outName = `out_${stamp}.${format}`;
        try {
          await ff.writeFile(inputName, await fetchFile(item.file));
          await ff.exec(this.codecArgs(format, inputName, outName));
          const data = await ff.readFile(outName);
          if (item.url) URL.revokeObjectURL(item.url);
          item.url = URL.createObjectURL(new Blob([data.buffer], { type: 'audio/' + format }));
          item.outName = `${base}.${format}`;
          item.state = 'done';
          item.error = '';
          await ff.deleteFile(inputName).catch(() => {});
          await ff.deleteFile(outName).catch(() => {});
        } catch (e) {
          if (this.cancelled) { item.state = 'queued'; break; }
          item.state = 'error';
          item.error = e.message || String(e);
        }
        this.render();
      }

      const done = this.files.filter(f => f.state === 'done').length;
      status.textContent = this.cancelled
        ? `Cancelled. ${done} file(s) finished before stopping.`
        : `Done. ${done} of ${this.files.length} file(s) converted.`;
    } catch (e) {
      status.textContent = 'Engine failed to load: ' + (e.message || e);
    } finally {
      this.running = false;
      document.getElementById('convert-start').disabled = !this.files.length;
      document.getElementById('convert-cancel').style.display = 'none';
      this.render();
    }
  },

  cancel() {
    this.cancelled = true;
    try { if (this.ffmpeg) this.ffmpeg.terminate(); } catch (e) {}
    this.ffmpeg = null;
  },

  clearDone() {
    this.files.forEach(f => { if (f.url) URL.revokeObjectURL(f.url); });
    this.files = this.files.filter(f => f.state !== 'done');
    this.render();
  },

  remove(i) {
    const f = this.files[i];
    if (f && f.url) URL.revokeObjectURL(f.url);
    this.files.splice(i, 1);
    this.render();
  },

  render() {
    const el = document.getElementById('convert-results');
    if (!el) return;
    if (!this.files.length) { el.innerHTML = ''; document.getElementById('convert-start').disabled = true; return; }
    document.getElementById('convert-start').disabled = this.running;

    const rows = this.files.map((f, i) => {
      const label = f.state === 'done' ? 'ready' : f.state === 'error' ? 'failed' : f.state;
      const download = f.state === 'done'
        ? `<a class="btn primary sm" href="${f.url}" download="${esc(f.outName)}">Save</a>` : '';
      return `
        <div class="convert-file">
          <span class="convert-name">${esc(f.file.name)} <span class="muted">(${(f.file.size / 1048576).toFixed(1)} MB)</span></span>
          <span class="convert-state ${f.state}">${esc(label)}</span>
          ${f.state === 'converting' ? `<div class="convert-progress"><div class="convert-progress-fill" style="width:${Math.round(f.progress * 100)}%"></div></div>` : ''}
          ${download}
          <button class="btn ghost sm" data-remove="${i}">×</button>
        </div>`;
    }).join('');

    el.innerHTML = `<div class="card">
      <div style="display:flex;align-items:center;margin-bottom:8px">
        <strong style="flex:1">${this.files.length} file(s)</strong>
        <button class="btn ghost sm" id="convert-clear-done">Clear finished</button>
      </div>${rows}</div>`;

    el.querySelectorAll('[data-remove]').forEach(b =>
      b.addEventListener('click', () => this.remove(parseInt(b.dataset.remove))));
    const clearBtn = document.getElementById('convert-clear-done');
    if (clearBtn) clearBtn.addEventListener('click', () => this.clearDone());
  },
};
