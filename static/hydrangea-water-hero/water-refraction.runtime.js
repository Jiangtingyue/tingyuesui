/**
 * JTYHome v8.9.8 · Same-origin WebGL refraction + CPU height-field water.
 * Pointer samples are coalesced once per frame, interpolated into a continuous
 * trail and bounded by a backend-negotiated performance profile.
 */
(function () {
  'use strict';

  const VERTEX_SHADER = `
attribute vec2 aPosition;
varying vec2 vUv;

void main() {
  vUv = aPosition * 0.5 + 0.5;
  gl_Position = vec4(aPosition, 0.0, 1.0);
}
`;

  const REFRACTION_FRAGMENT_SHADER = `
precision highp float;
varying vec2 vUv;
uniform sampler2D uVideo;
uniform sampler2D uSim;
uniform vec2 uTexel;
uniform vec2 uFrac;
uniform float uTime;

void main() {
  float leftH = texture2D(uSim, vUv - vec2(uTexel.x, 0.0)).r;
  float rightH = texture2D(uSim, vUv + vec2(uTexel.x, 0.0)).r;
  float downH = texture2D(uSim, vUv - vec2(0.0, uTexel.y)).r;
  float upH = texture2D(uSim, vUv + vec2(0.0, uTexel.y)).r;
  vec2 grad = vec2(rightH - leftH, upH - downH);

  vec2 uv = (vUv - 0.5) * uFrac + 0.5;
  uv += grad * 0.42;
  uv = clamp(uv, vec2(0.002), vec2(0.998));

  vec3 color = texture2D(uVideo, uv).rgb * 0.9;
  float light = (grad.x + grad.y) * 2.4;
  color += light * vec3(1.0, 0.98, 0.92);
  color += max(0.0, light - 0.045) * 5.5 * vec3(1.0, 1.0, 0.96);
  color += sin(uTime * 0.45 + vUv.y * 8.0) * 0.0025
    * max(0.0, light) * vec3(1.0, 0.98, 0.92);
  gl_FragColor = vec4(color, 1.0);
}
`;

  const WATER_DAMP = 0.979;
  const DEFAULT_CONFIG = Object.freeze({
    name: 'balanced',
    sim_width: 160,
    sim_max_height: 320,
    max_dpr: 1.75,
    target_fps: 60,
    pointer_interval_ms: 10,
    trail_spacing_px: 6,
    trail_radius: 2.5,
    trail_strength: 0.44,
    tap_radius: 4.8,
    tap_strength: 0.72,
    max_pointer_samples: 24,
  });

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function finite(value, fallback, min, max) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return fallback;
    return clamp(parsed, min, max);
  }

  function normalizeConfig(value) {
    const source = value && typeof value === 'object' ? value : {};
    return {
      name: String(source.name || DEFAULT_CONFIG.name).slice(0, 24),
      sim_width: Math.round(finite(source.sim_width, DEFAULT_CONFIG.sim_width, 96, 256)),
      sim_max_height: Math.round(finite(source.sim_max_height, DEFAULT_CONFIG.sim_max_height, 160, 512)),
      max_dpr: finite(source.max_dpr, DEFAULT_CONFIG.max_dpr, 1, 2.5),
      target_fps: Math.round(finite(source.target_fps, DEFAULT_CONFIG.target_fps, 24, 60)),
      pointer_interval_ms: finite(source.pointer_interval_ms, DEFAULT_CONFIG.pointer_interval_ms, 4, 30),
      trail_spacing_px: finite(source.trail_spacing_px, DEFAULT_CONFIG.trail_spacing_px, 3, 18),
      trail_radius: finite(source.trail_radius, DEFAULT_CONFIG.trail_radius, 1.4, 5),
      trail_strength: finite(source.trail_strength, DEFAULT_CONFIG.trail_strength, 0.12, 1.2),
      tap_radius: finite(source.tap_radius, DEFAULT_CONFIG.tap_radius, 2.5, 9),
      tap_strength: finite(source.tap_strength, DEFAULT_CONFIG.tap_strength, 0.2, 1.5),
      max_pointer_samples: Math.round(finite(source.max_pointer_samples, DEFAULT_CONFIG.max_pointer_samples, 8, 48)),
    };
  }

  function createShader(gl, type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      const message = gl.getShaderInfoLog(shader) || 'Unknown shader compile error';
      gl.deleteShader(shader);
      throw new Error(message);
    }
    return shader;
  }

  function createProgram(gl, vertexSource, fragmentSource) {
    const program = gl.createProgram();
    const vertex = createShader(gl, gl.VERTEX_SHADER, vertexSource);
    const fragment = createShader(gl, gl.FRAGMENT_SHADER, fragmentSource);
    gl.attachShader(program, vertex);
    gl.attachShader(program, fragment);
    gl.linkProgram(program);
    gl.deleteShader(vertex);
    gl.deleteShader(fragment);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      const message = gl.getProgramInfoLog(program) || 'Unknown program link error';
      gl.deleteProgram(program);
      throw new Error(message);
    }
    return program;
  }

  function createTexture(gl, width, height, format, bytes) {
    const texture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texImage2D(gl.TEXTURE_2D, 0, format, width, height, 0, format, gl.UNSIGNED_BYTE, bytes);
    return texture;
  }

  function initVideoRefraction(options) {
    const opts = options || {};
    const canvas = opts.canvas;
    const image = opts.image || null;
    const video = opts.video || null;
    const media = image || video;
    const isImage = Boolean(image);
    const isActive = opts.isActive || function () { return true; };
    const onFailure = opts.onFailure || function () {};
    const onRendererChange = opts.onRendererChange || function () {};
    if (!canvas || !media) return null;

    let runtimeConfig = normalizeConfig(opts.config);
    let gl = null;
    let refractionProgram = null;
    let quad = null;
    let mediaTexture = null;
    let simTexture = null;
    let uniforms = null;
    let simWidth = runtimeConfig.sim_width;
    let simHeight = Math.max(72, Math.round(simWidth * 0.625));
    let wave = new Float32Array(simWidth * simHeight);
    let wavePrev = new Float32Array(simWidth * simHeight);
    let simBytes = new Uint8Array(simWidth * simHeight);
    simBytes.fill(128);
    let raf = 0;
    let running = false;
    let contextLost = false;
    let startTime = performance.now();
    let lastRenderAt = 0;
    let mediaTextureUploaded = false;
    const pointerStates = new Map();
    let pointerSamples = [];
    let cachedBounds = null;

    function surfaceSize(refresh) {
      if (!refresh && cachedBounds) return cachedBounds;
      const rect = canvas.getBoundingClientRect();
      cachedBounds = {
        left: Number.isFinite(rect.left) ? rect.left : 0,
        top: Number.isFinite(rect.top) ? rect.top : 0,
        width: Math.max(1, rect.width || window.innerWidth || 1),
        height: Math.max(1, rect.height || window.innerHeight || 1),
      };
      return cachedBounds;
    }

    function allocateSimulation(width, height) {
      simWidth = width;
      simHeight = height;
      wave = new Float32Array(simWidth * simHeight);
      wavePrev = new Float32Array(simWidth * simHeight);
      simBytes = new Uint8Array(simWidth * simHeight);
      simBytes.fill(128);
      if (gl && simTexture) {
        gl.activeTexture(gl.TEXTURE1);
        gl.bindTexture(gl.TEXTURE_2D, simTexture);
        gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
        gl.texImage2D(
          gl.TEXTURE_2D, 0, gl.LUMINANCE, simWidth, simHeight, 0,
          gl.LUMINANCE, gl.UNSIGNED_BYTE, simBytes
        );
      }
    }

    function resize(force) {
      if (!gl) return;
      const bounds = surfaceSize(Boolean(force));
      const dpr = Math.min(window.devicePixelRatio || 1, runtimeConfig.max_dpr);
      const width = Math.max(1, Math.round(bounds.width * dpr));
      const height = Math.max(1, Math.round(bounds.height * dpr));
      if (force || canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      const nextWidth = runtimeConfig.sim_width;
      const nextHeight = clamp(
        Math.round(nextWidth * (bounds.height / bounds.width)),
        72,
        runtimeConfig.sim_max_height
      );
      if (force || simWidth !== nextWidth || simHeight !== nextHeight) {
        allocateSimulation(nextWidth, nextHeight);
      }
    }

    function destroyResources() {
      if (!gl) return;
      if (refractionProgram) gl.deleteProgram(refractionProgram);
      if (quad) gl.deleteBuffer(quad);
      if (mediaTexture) gl.deleteTexture(mediaTexture);
      if (simTexture) gl.deleteTexture(simTexture);
      refractionProgram = null;
      quad = null;
      mediaTexture = null;
      simTexture = null;
      uniforms = null;
    }

    function status(reason) {
      return {
        renderer: gl && !contextLost ? 'webgl' : 'image',
        running: running,
        context_lost: contextLost,
        quality: runtimeConfig.name,
        sim_width: simWidth,
        sim_height: simHeight,
        target_fps: runtimeConfig.target_fps,
        reason: reason || '',
      };
    }

    function setup() {
      try {
        gl = canvas.getContext('webgl', {
          alpha: true,
          antialias: false,
          depth: false,
          stencil: false,
          premultipliedAlpha: false,
          preserveDrawingBuffer: false,
          powerPreference: 'high-performance',
        });
        if (!gl) throw new Error('WebGL unavailable');

        refractionProgram = createProgram(gl, VERTEX_SHADER, REFRACTION_FRAGMENT_SHADER);
        quad = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, quad);
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([
          -1, -1,
           1, -1,
          -1,  1,
           1,  1,
        ]), gl.STATIC_DRAW);

        mediaTexture = createTexture(gl, 2, 2, gl.RGBA, null);
        simTexture = createTexture(gl, simWidth, simHeight, gl.LUMINANCE, simBytes);
        uniforms = {
          position: gl.getAttribLocation(refractionProgram, 'aPosition'),
          media: gl.getUniformLocation(refractionProgram, 'uVideo'),
          sim: gl.getUniformLocation(refractionProgram, 'uSim'),
          texel: gl.getUniformLocation(refractionProgram, 'uTexel'),
          frac: gl.getUniformLocation(refractionProgram, 'uFrac'),
          time: gl.getUniformLocation(refractionProgram, 'uTime'),
        };
        gl.disable(gl.DEPTH_TEST);
        gl.disable(gl.CULL_FACE);
        mediaTextureUploaded = false;
        resize(true);
        onRendererChange('webgl', status());
        return true;
      } catch (error) {
        destroyResources();
        gl = null;
        onFailure(error);
        onRendererChange('image', status(error && error.message));
        return false;
      }
    }

    function coverFrac() {
      const bounds = surfaceSize();
      const viewportAspect = bounds.width / bounds.height;
      const mediaWidth = isImage ? image.naturalWidth : video.videoWidth;
      const mediaHeight = isImage ? image.naturalHeight : video.videoHeight;
      const mediaAspect = mediaWidth && mediaHeight ? mediaWidth / mediaHeight : viewportAspect;
      return [
        Math.min(1, viewportAspect / mediaAspect),
        Math.min(1, mediaAspect / viewportAspect),
      ];
    }

    function drop(px, py, radius, strength) {
      const bounds = surfaceSize();
      const localX = px - bounds.left;
      const localY = py - bounds.top;
      if (localX < -1 || localX > bounds.width + 1 || localY < -1 || localY > bounds.height + 1) return;
      const sx = clamp((localX / bounds.width) * (simWidth - 1), 0, simWidth - 1);
      const sy = clamp((1 - localY / bounds.height) * (simHeight - 1), 0, simHeight - 1);
      const x0 = Math.max(1, Math.floor(sx - radius));
      const x1 = Math.min(simWidth - 2, Math.ceil(sx + radius));
      const y0 = Math.max(1, Math.floor(sy - radius));
      const y1 = Math.min(simHeight - 2, Math.ceil(sy + radius));
      const invRadius = 1 / Math.max(0.0001, radius);
      for (let y = y0; y <= y1; y += 1) {
        for (let x = x0; x <= x1; x += 1) {
          const dx = x - sx;
          const dy = y - sy;
          const dist = Math.sqrt(dx * dx + dy * dy);
          if (dist > radius) continue;
          const falloff = Math.cos((dist * invRadius) * Math.PI * 0.5);
          wave[y * simWidth + x] += strength * falloff * falloff;
        }
      }
    }

    function gradAt(px, py) {
      const bounds = surfaceSize();
      const localX = px - bounds.left;
      const localY = py - bounds.top;
      const sx = clamp(Math.round((localX / bounds.width) * (simWidth - 1)), 1, simWidth - 2);
      const sy = clamp(Math.round((1 - localY / bounds.height) * (simHeight - 1)), 1, simHeight - 2);
      const i = sy * simWidth + sx;
      return {
        x: wave[i + 1] - wave[i - 1],
        y: wave[i + simWidth] - wave[i - simWidth],
      };
    }

    function stepWater() {
      const width = simWidth;
      const height = simHeight;
      for (let y = 1; y < height - 1; y += 1) {
        const row = y * width;
        for (let x = 1; x < width - 1; x += 1) {
          const i = row + x;
          wavePrev[i] = (
            (wave[i - 1] + wave[i + 1] + wave[i - width] + wave[i + width]) * 0.5
            - wavePrev[i]
          ) * WATER_DAMP;
        }
      }
      const temp = wave;
      wave = wavePrev;
      wavePrev = temp;
    }

    function packAndUploadSimulation() {
      for (let i = 0; i < wave.length; i += 1) {
        simBytes[i] = clamp(128 + wave[i] * 26, 1, 254);
      }
      gl.activeTexture(gl.TEXTURE1);
      gl.bindTexture(gl.TEXTURE_2D, simTexture);
      gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
      gl.texSubImage2D(
        gl.TEXTURE_2D, 0, 0, 0, simWidth, simHeight,
        gl.LUMINANCE, gl.UNSIGNED_BYTE, simBytes
      );
    }

    function uploadMediaFrame() {
      if (isImage) {
        if (!image.complete || !image.naturalWidth || !image.naturalHeight) return false;
        if (mediaTextureUploaded) return true;
      } else if (video.readyState < 2 || !video.videoWidth || !video.videoHeight) {
        return false;
      }
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, mediaTexture);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
      try {
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, media);
        if (isImage) mediaTextureUploaded = true;
        return true;
      } catch (error) {
        onFailure(error);
        return false;
      }
    }

    function renderRefraction(time) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.useProgram(refractionProgram);
      gl.bindBuffer(gl.ARRAY_BUFFER, quad);
      gl.enableVertexAttribArray(uniforms.position);
      gl.vertexAttribPointer(uniforms.position, 2, gl.FLOAT, false, 0, 0);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, mediaTexture);
      gl.uniform1i(uniforms.media, 0);
      gl.activeTexture(gl.TEXTURE1);
      gl.bindTexture(gl.TEXTURE_2D, simTexture);
      gl.uniform1i(uniforms.sim, 1);
      gl.uniform2f(uniforms.texel, 1 / simWidth, 1 / simHeight);
      const frac = coverFrac();
      gl.uniform2f(uniforms.frac, frac[0], frac[1]);
      gl.uniform1f(uniforms.time, time);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }

    function sampleFromEvent(event, phase) {
      return {
        id: Number.isFinite(event.pointerId) ? event.pointerId : 1,
        x: event.clientX,
        y: event.clientY,
        time: Number.isFinite(event.timeStamp) ? event.timeStamp : performance.now(),
        pressure: Number.isFinite(event.pressure) && event.pressure > 0 ? event.pressure : 0.5,
        pointerType: event.pointerType || 'mouse',
        phase: phase,
      };
    }

    function queueSample(sample) {
      pointerSamples.push(sample);
      const limit = runtimeConfig.max_pointer_samples;
      if (pointerSamples.length > limit) {
        pointerSamples.splice(0, pointerSamples.length - limit);
      }
    }

    function applyPointerSamples() {
      if (!pointerSamples.length) return;
      const samples = pointerSamples;
      pointerSamples = [];
      samples.forEach(function (sample) {
        if (sample.phase === 'up') {
          pointerStates.delete(sample.id);
          return;
        }
        const previous = pointerStates.get(sample.id);
        if (sample.phase === 'down') {
          drop(sample.x, sample.y, runtimeConfig.tap_radius, runtimeConfig.tap_strength);
          pointerStates.set(sample.id, sample);
          return;
        }
        if (!previous) {
          pointerStates.set(sample.id, sample);
          return;
        }
        const dx = sample.x - previous.x;
        const dy = sample.y - previous.y;
        const distance = Math.hypot(dx, dy);
        const elapsed = Math.max(1, sample.time - previous.time);
        if (distance < 1.25 || elapsed < runtimeConfig.pointer_interval_ms) return;
        const velocity = distance / elapsed;
        const spacing = runtimeConfig.trail_spacing_px;
        const steps = Math.min(12, Math.max(1, Math.ceil(distance / spacing)));
        const pressureScale = 0.82 + clamp(sample.pressure, 0, 1) * 0.36;
        const velocityScale = 0.82 + clamp(velocity * 0.12, 0, 0.38);
        const strength = runtimeConfig.trail_strength * pressureScale * velocityScale;
        const radius = runtimeConfig.trail_radius * (0.92 + clamp(velocity * 0.06, 0, 0.24));
        for (let index = 1; index <= steps; index += 1) {
          const ratio = index / steps;
          drop(
            previous.x + dx * ratio,
            previous.y + dy * ratio,
            radius,
            strength
          );
        }
        pointerStates.set(sample.id, sample);
      });
    }

    function frame(now) {
      raf = 0;
      if (!running || contextLost || document.hidden) return;
      if (!isActive()) return;
      const frameInterval = 1000 / runtimeConfig.target_fps;
      if (lastRenderAt && now - lastRenderAt < frameInterval * 0.88) {
        raf = requestAnimationFrame(frame);
        return;
      }
      const elapsed = lastRenderAt ? now - lastRenderAt : frameInterval;
      lastRenderAt = now;
      resize(false);
      applyPointerSamples();
      const simulationSteps = clamp(Math.round(elapsed / (1000 / 60)), 1, 3);
      for (let index = 0; index < simulationSteps; index += 1) stepWater();
      packAndUploadSimulation();
      if (uploadMediaFrame()) renderRefraction((now - startTime) * 0.001);
      raf = requestAnimationFrame(frame);
    }

    function onPointerDown(event) {
      if (!isActive()) return;
      try { canvas.setPointerCapture(event.pointerId); } catch (_) { /* optional */ }
      const sample = sampleFromEvent(event, 'down');
      pointerStates.set(sample.id, sample);
      drop(sample.x, sample.y, runtimeConfig.tap_radius, runtimeConfig.tap_strength);
    }

    function onPointerMove(event) {
      if (!isActive()) return;
      if (event.pointerType === 'touch' && !pointerStates.has(event.pointerId)) return;
      const coalesced = typeof event.getCoalescedEvents === 'function'
        ? event.getCoalescedEvents()
        : [];
      const events = coalesced.length ? coalesced : [event];
      const start = Math.max(0, events.length - runtimeConfig.max_pointer_samples);
      for (let index = start; index < events.length; index += 1) {
        queueSample(sampleFromEvent(events[index], 'move'));
      }
    }

    function onPointerEnd(event) {
      queueSample(sampleFromEvent(event, 'up'));
      try { canvas.releasePointerCapture(event.pointerId); } catch (_) { /* optional */ }
    }

    function onPointerLeave(event) {
      if ((event.pointerType || 'mouse') === 'mouse') {
        pointerStates.delete(event.pointerId);
      }
    }

    function onVisibilityChange() {
      if (!running) return;
      if (document.hidden) {
        if (raf) cancelAnimationFrame(raf);
        raf = 0;
        pointerSamples = [];
        pointerStates.clear();
      } else if (!contextLost && isActive() && !raf) {
        lastRenderAt = 0;
        raf = requestAnimationFrame(frame);
      }
    }

    function start() {
      if (running && gl && !contextLost) return true;
      running = true;
      startTime = performance.now();
      lastRenderAt = 0;
      if (!gl && !contextLost && !setup()) {
        running = false;
        return false;
      }
      if (!document.hidden && !raf) raf = requestAnimationFrame(frame);
      return true;
    }

    function stop() {
      running = false;
      pointerSamples = [];
      pointerStates.clear();
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      lastRenderAt = 0;
      if (gl && !contextLost) {
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        gl.viewport(0, 0, canvas.width, canvas.height);
        gl.clearColor(0, 0, 0, 0);
        gl.clear(gl.COLOR_BUFFER_BIT);
      }
    }

    function applyConfig(next) {
      runtimeConfig = normalizeConfig(Object.assign({}, runtimeConfig, next || {}));
      if (gl && !contextLost) resize(true);
      return status();
    }

    function onContextLost(event) {
      event.preventDefault();
      contextLost = true;
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      pointerSamples = [];
      pointerStates.clear();
      const error = new Error('WebGL context lost');
      onFailure(error);
      onRendererChange('image', status(error.message));
    }

    function onContextRestored() {
      contextLost = false;
      destroyResources();
      gl = null;
      if (!setup()) return;
      if (running && !document.hidden && isActive() && !raf) {
        startTime = performance.now();
        lastRenderAt = 0;
        raf = requestAnimationFrame(frame);
      }
    }

    canvas.addEventListener('pointerdown', onPointerDown, { passive: true });
    canvas.addEventListener('pointermove', onPointerMove, { passive: true });
    canvas.addEventListener('pointerup', onPointerEnd, { passive: true });
    canvas.addEventListener('pointercancel', onPointerEnd, { passive: true });
    canvas.addEventListener('pointerleave', onPointerLeave, { passive: true });
    canvas.addEventListener('webglcontextlost', onContextLost, false);
    canvas.addEventListener('webglcontextrestored', onContextRestored, false);
    window.addEventListener('resize', resize, { passive: true });
    document.addEventListener('visibilitychange', onVisibilityChange);

    return {
      start: start,
      stop: stop,
      resize: resize,
      drop: drop,
      gradAt: gradAt,
      applyConfig: applyConfig,
      getStatus: status,
      destroy: function () {
        stop();
        canvas.removeEventListener('pointerdown', onPointerDown);
        canvas.removeEventListener('pointermove', onPointerMove);
        canvas.removeEventListener('pointerup', onPointerEnd);
        canvas.removeEventListener('pointercancel', onPointerEnd);
        canvas.removeEventListener('pointerleave', onPointerLeave);
        canvas.removeEventListener('webglcontextlost', onContextLost);
        canvas.removeEventListener('webglcontextrestored', onContextRestored);
        window.removeEventListener('resize', resize);
        document.removeEventListener('visibilitychange', onVisibilityChange);
        destroyResources();
        gl = null;
      },
    };
  }

  window.JTYHydrangeaVideoRefraction = Object.freeze({
    initVideoRefraction: initVideoRefraction,
    DEFAULT_CONFIG: DEFAULT_CONFIG,
    VERTEX_SHADER: VERTEX_SHADER,
    REFRACTION_FRAGMENT_SHADER: REFRACTION_FRAGMENT_SHADER,
  });
})();
