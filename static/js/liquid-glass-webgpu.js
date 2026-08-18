/**
 * 大西瓜 8.9.3 · WebGPU liquid glass backend.
 *
 * WebGPU is preferred when the browser exposes a usable adapter in a secure
 * context. The legacy WebGL renderer waits for `ready` and becomes the exact
 * fallback, so the two backends can never paint the same card together.
 */
(() => {
  'use strict';

  const root = document.documentElement;
  const MOBILE_BREAKPOINT = 768;
  const MAX_TARGETS = 64;
  const DPR_LIMIT = 1.25;
  const MAX_BACKBUFFER_PIXELS = 1_800_000;
  const UNIFORM_FLOATS = 24;
  const UNIFORM_BYTES = UNIFORM_FLOATS * 4;
  const TARGET_SELECTORS = {
    desktop: [
      '#view-home .home-hero .hero-copy',
      '.sidebar-companion-card',
      '.dwell-life-hero',
      '.intimacy-vitals:not([hidden])',
      '.v872-glass-target',
      '.jty-space-glass',
      '.jty-jelly-target',
      '.jty-3d-glass-target',
    ],
  };

  const ALL_TARGET_QUERY = Array.from(new Set(TARGET_SELECTORS.desktop)).join(',');

  const SHADER_SOURCE = /* wgsl */ `
    struct Uniforms {
      viewport: vec2<f32>,
      imageSize: vec2<f32>,
      targetRect: vec4<f32>,
      material: vec4<f32>,
      pointer: vec4<f32>,
      optics: vec4<f32>,
      lightScene: vec4<f32>,
    };

    @group(0) @binding(0) var<uniform> u: Uniforms;
    @group(0) @binding(1) var sceneSampler: sampler;
    @group(0) @binding(2) var sceneTexture: texture_2d<f32>;

    struct VertexOutput {
      @builtin(position) position: vec4<f32>,
    };

    @vertex
    fn vertexMain(@builtin(vertex_index) index: u32) -> VertexOutput {
      var positions = array<vec2<f32>, 6>(
        vec2<f32>(-1.0, -1.0), vec2<f32>(1.0, -1.0), vec2<f32>(-1.0, 1.0),
        vec2<f32>(-1.0, 1.0), vec2<f32>(1.0, -1.0), vec2<f32>(1.0, 1.0)
      );
      var output: VertexOutput;
      output.position = vec4<f32>(positions[index], 0.0, 1.0);
      return output;
    }

    fn coverUv(screenPx: vec2<f32>) -> vec2<f32> {
      let scale = max(
        u.viewport.x / max(u.imageSize.x, 1.0),
        u.viewport.y / max(u.imageSize.y, 1.0)
      );
      let rendered = max(u.imageSize * scale, vec2<f32>(1.0));
      let position = vec2<f32>(u.lightScene.z, 0.5);
      let crop = (rendered - u.viewport) * position;
      return clamp((screenPx + crop) / rendered, vec2<f32>(0.001), vec2<f32>(0.999));
    }

    fn sampleScene(screenPx: vec2<f32>) -> vec3<f32> {
      // Explicit LOD keeps sampling valid at the non-uniform rounded-corner
      // edge where some fragments return transparent before their neighbours.
      return textureSampleLevel(sceneTexture, sceneSampler, coverUv(screenPx), 0.0).rgb;
    }

    fn sampleBlur(screenPx: vec2<f32>, spread: f32) -> vec3<f32> {
      let axisX = vec2<f32>(spread, 0.0);
      let axisY = vec2<f32>(0.0, spread);
      let diagonal = vec2<f32>(spread * 0.70710678);
      var color = sampleScene(screenPx) * 0.24;
      color += (
        sampleScene(screenPx + axisX) + sampleScene(screenPx - axisX)
        + sampleScene(screenPx + axisY) + sampleScene(screenPx - axisY)
      ) * 0.12;
      color += (
        sampleScene(screenPx + diagonal) + sampleScene(screenPx - diagonal)
        + sampleScene(screenPx + vec2<f32>(-diagonal.x, diagonal.y))
        + sampleScene(screenPx + vec2<f32>(diagonal.x, -diagonal.y))
      ) * 0.07;
      return color;
    }

    fn roundedSdf(
      point: vec2<f32>, center: vec2<f32>, halfSize: vec2<f32>, radius: f32
    ) -> f32 {
      let safeRadius = clamp(radius, 0.0, min(halfSize.x, halfSize.y));
      let q = abs(point - center) - max(halfSize - vec2<f32>(safeRadius), vec2<f32>(0.0));
      return length(max(q, vec2<f32>(0.0)))
        + min(max(q.x, q.y), 0.0) - safeRadius;
    }

    fn roundedNormal(
      point: vec2<f32>, center: vec2<f32>, halfSize: vec2<f32>, radius: f32
    ) -> vec2<f32> {
      let epsilon = max(0.8, u.lightScene.w);
      let gradient = vec2<f32>(
        roundedSdf(point + vec2<f32>(epsilon, 0.0), center, halfSize, radius)
          - roundedSdf(point - vec2<f32>(epsilon, 0.0), center, halfSize, radius),
        roundedSdf(point + vec2<f32>(0.0, epsilon), center, halfSize, radius)
          - roundedSdf(point - vec2<f32>(0.0, epsilon), center, halfSize, radius)
      );
      if (length(gradient) < 0.0001) {
        return vec2<f32>(0.0, -1.0);
      }
      return normalize(gradient);
    }

    @fragment
    fn fragmentMain(input: VertexOutput) -> @location(0) vec4<f32> {
      let screenPx = input.position.xy;
      if (u.material.y < 0.5) {
        return vec4<f32>(sampleScene(screenPx), 1.0);
      }

      let targetCenter = u.targetRect.xy + u.targetRect.zw * 0.5;
      let targetHalf = max(u.targetRect.zw * 0.5, vec2<f32>(1.0));
      let radius = min(u.material.x, min(targetHalf.x, targetHalf.y));
      let sdf = roundedSdf(screenPx, targetCenter, targetHalf, radius);
      let dpr = max(u.lightScene.w, 0.75);
      let mask = 1.0 - smoothstep(-1.15 * dpr, 0.85 * dpr, sdf);
      if (mask <= 0.001) {
        return vec4<f32>(0.0);
      }

      let depth = max(-sdf, 0.0);
      let normal = roundedNormal(screenPx, targetCenter, targetHalf, radius);
      let relative = (screenPx - targetCenter) / targetHalf;
      let iosMaterial = select(0.0, 1.0, u.material.z >= 0.5 && u.material.z < 1.5);
      let jellyMaterial = select(0.0, 1.0, u.material.z >= 1.5);
      let rimWidth = mix(34.0, 27.0, iosMaterial) * dpr + jellyMaterial * 9.0 * dpr;
      let rim = 1.0 - smoothstep(0.75 * dpr, rimWidth, depth);
      let crystalEdge = smoothstep(0.08, 0.92, rim);

      let pointerDistance = distance(screenPx, u.pointer.xy);
      let pointerLift = u.pointer.z * (1.0 - smoothstep(22.0 * dpr, 180.0 * dpr, pointerDistance));
      let edgeRefraction = mix(
        4.6 + crystalEdge * 6.0,
        8.2 + crystalEdge * 14.0,
        iosMaterial
      ) * u.optics.x * dpr;
      let wave = vec2<f32>(
        sin(relative.y * 5.7 + u.material.w * 0.16),
        cos(relative.x * 4.1 - u.material.w * 0.12)
      ) * jellyMaterial * (0.7 + rim * 1.4) * dpr;
      let refractedPx = screenPx + normal * rim
        * (edgeRefraction + pointerLift * 3.0 * dpr) + wave;

      let chroma = rim * mix(0.30, 0.78, iosMaterial) * dpr * (1.0 - jellyMaterial * 0.75);
      var refracted = vec3<f32>(
        sampleScene(refractedPx + normal * chroma).r,
        sampleScene(refractedPx).g,
        sampleScene(refractedPx - normal * chroma).b
      );

      let contentField = 1.0 - smoothstep(0.16, 0.72, rim);
      let blurSpread = u.pointer.w + iosMaterial * 0.65 * dpr + jellyMaterial * 1.2 * dpr;
      var calm = sampleBlur(refractedPx, blurSpread);
      let calmLum = dot(calm, vec3<f32>(0.2126, 0.7152, 0.0722));
      calm = mix(vec3<f32>(calmLum), calm, 0.70);
      calm = (calm - vec3<f32>(0.5)) * 0.90 + vec3<f32>(0.5);
      calm = mix(calm, vec3<f32>(0.972, 0.985, 0.982), 0.095);
      refracted = mix(refracted, calm, contentField * mix(0.42, 0.35, jellyMaterial));

      let readabilityLift = contentField * clamp(
        0.060 + (0.60 - calmLum) * 0.12,
        0.048,
        0.115
      );
      refracted = mix(refracted, vec3<f32>(0.976, 0.988, 0.985), readabilityLift);

      let lightDirection = normalize(u.lightScene.xy);
      let reflected = sampleScene(screenPx - lightDirection * (5.0 + rim * 12.0) * dpr);
      let fresnel = pow(clamp(rim, 0.0, 1.0), mix(1.55, 1.30, jellyMaterial))
        * u.optics.y * u.optics.w;
      refracted = mix(refracted, reflected * 1.012, fresnel * u.optics.z);

      let outerContour = 1.0 - smoothstep(0.25 * dpr, 4.2 * dpr, depth);
      let innerCrease = smoothstep(3.0 * dpr, 5.0 * dpr, depth)
        * (1.0 - smoothstep(5.0 * dpr, 9.2 * dpr, depth));
      let contourLight = pow(max(dot(-normal, lightDirection), 0.0), 4.2);
      let contourShade = pow(max(dot(normal, lightDirection), 0.0), 2.7);
      refracted += vec3<f32>(0.34, 0.42, 0.43)
        * outerContour * (0.22 + contourLight * 0.43) * u.optics.w;
      refracted -= vec3<f32>(innerCrease * (0.030 + contourShade * 0.042));

      let topLight = pow(max(dot(-normal, lightDirection), 0.0), 6.0) * rim;
      let lowerShade = pow(max(dot(normal, lightDirection), 0.0), 4.0) * rim;
      refracted += topLight * vec3<f32>(0.12, 0.145, 0.145);
      refracted -= lowerShade * vec3<f32>(0.020, 0.028, 0.030);
      refracted = mix(refracted, vec3<f32>(0.90, 1.0, 0.995), rim * 0.028);

      return vec4<f32>(clamp(refracted, vec3<f32>(0.0), vec3<f32>(1.0)), mask);
    }
  `;

  const api = {
    backend: 'webgpu',
    active: false,
    reason: 'initializing',
    ready: null,
  };
  window.JTYLiquidGlassWebGPU = api;

  let adapter;
  let device;
  let context;
  let format;
  let canvas;
  let pipeline;
  let bindGroupLayout;
  let bindGroup;
  let uniformBuffer;
  let uniformStride = 256;
  let sampler;
  let sceneTexture;
  let sceneImage;
  let sceneKey = '';
  let loadingKey = '';
  let loadingPromise = null;
  let targetNodes = [];
  let resizeObserver;
  let mutationObserver;
  let domMutationObserver;
  let raf = 0;
  let refreshPending = false;
  let pointerX = -10000;
  let pointerY = -10000;
  let pointerTarget = null;
  let effectiveDpr = 1;
  let disposed = false;
  let uiMotionUntil = 0;

  function isMobile() {
    return window.innerWidth <= MOBILE_BREAKPOINT;
  }

  function isRenderActive() {
    return Boolean(document.body) && root.dataset.uiMode !== 'reader';
  }

  function waitForDom() {
    if (document.readyState !== 'loading') return Promise.resolve();
    return new Promise((resolve) => {
      document.addEventListener('DOMContentLoaded', resolve, { once: true });
    });
  }

  function currentImageUrl() {
    const raining = root.dataset.weather === 'rain' || root.dataset.weather === 'storm';
    const suffix = isMobile() ? 'mobile' : 'desktop';
    return `/static/images/${raining ? 'crystal' : 'sunny'}-street-${suffix}.jpg`;
  }

  function loadImage(url) {
    return new Promise((resolve, reject) => {
      const image = new Image();
      image.decoding = 'async';
      image.onload = () => resolve(image);
      image.onerror = () => reject(new Error(`无法载入 WebGPU 背景：${url}`));
      image.src = url;
    });
  }

  async function ensureTexture() {
    const wanted = currentImageUrl();
    if (sceneTexture && sceneImage && sceneKey === wanted) return;
    if (!loadingPromise || loadingKey !== wanted) {
      loadingKey = wanted;
      loadingPromise = loadImage(wanted);
    }
    const image = await loadingPromise;
    if (wanted !== currentImageUrl()) {
      loadingPromise = null;
      return ensureTexture();
    }

    const nextTexture = device.createTexture({
      label: 'JTY liquid glass scene',
      size: [image.naturalWidth || image.width, image.naturalHeight || image.height, 1],
      format: 'rgba8unorm',
      usage: GPUTextureUsage.TEXTURE_BINDING | GPUTextureUsage.COPY_DST,
    });
    device.queue.copyExternalImageToTexture(
      { source: image },
      { texture: nextTexture },
      [image.naturalWidth || image.width, image.naturalHeight || image.height]
    );
    sceneTexture?.destroy();
    sceneTexture = nextTexture;
    sceneImage = image;
    sceneKey = wanted;
    loadingKey = '';
    loadingPromise = null;
    bindGroup = device.createBindGroup({
      label: 'JTY liquid glass resources',
      layout: bindGroupLayout,
      entries: [
        { binding: 0, resource: { buffer: uniformBuffer, offset: 0, size: UNIFORM_BYTES } },
        { binding: 1, resource: sampler },
        { binding: 2, resource: sceneTexture.createView() },
      ],
    });
  }

  function calculateDpr() {
    const native = Math.min(window.devicePixelRatio || 1, DPR_LIMIT);
    const area = Math.max(1, window.innerWidth * window.innerHeight);
    return Math.max(0.85, Math.min(native, Math.sqrt(MAX_BACKBUFFER_PIXELS / area)));
  }

  function resizeCanvas() {
    effectiveDpr = calculateDpr();
    const width = Math.max(1, Math.round(window.innerWidth * effectiveDpr));
    const height = Math.max(1, Math.round(window.innerHeight * effectiveDpr));
    if (canvas.width !== width) canvas.width = width;
    if (canvas.height !== height) canvas.height = height;
    canvas.style.width = `${window.innerWidth}px`;
    canvas.style.height = `${window.innerHeight}px`;
  }

  function visible(node) {
    if (!node?.isConnected || node.closest('[hidden]')) return false;
    const ownerView = node.closest('.view');
    if (ownerView && !ownerView.classList.contains('active')) return false;
    if (node.closest('.glass-view-outgoing')) return false;
    let current = node;
    while (current && current !== document.body) {
      const style = window.getComputedStyle(current);
      if (
        style.display === 'none'
        || style.visibility === 'hidden'
        || Number.parseFloat(style.opacity || '1') <= .01
      ) return false;
      current = current.parentElement;
    }
    return true;
  }

  function mutationTouchesGlassTarget(record) {
    if (record.type === 'attributes') {
      const node = record.target;
      return node instanceof Element && (
        node.matches(ALL_TARGET_QUERY)
        || Boolean(node.querySelector?.(ALL_TARGET_QUERY))
        || Boolean(node.closest?.(ALL_TARGET_QUERY))
        || node.matches('.view, dialog')
      );
    }
    return [...record.addedNodes, ...record.removedNodes].some((node) => (
      node instanceof Element
      && (node.matches(ALL_TARGET_QUERY) || Boolean(node.querySelector(ALL_TARGET_QUERY)))
    ));
  }

  function refreshTargets() {
    targetNodes.forEach((node) => {
      node.classList.remove('liquid-webgl-target', 'liquid-gpu-target');
    });
    resizeObserver?.disconnect();
    const selectors = TARGET_SELECTORS.desktop;
    const candidates = selectors
      .flatMap((selector) => Array.from(document.querySelectorAll(selector)))
      .filter((node, index, list) => list.indexOf(node) === index)
      .filter(visible);
    const candidateSet = new Set(candidates);
    targetNodes = candidates.filter((node) => {
      let parent = node.parentElement;
      while (parent) {
        if (candidateSet.has(parent)) return false;
        parent = parent.parentElement;
      }
      return true;
    }).slice(0, MAX_TARGETS);
    targetNodes.forEach((node) => {
      // Keep the old class as a compatibility contract for the existing CSS.
      node.classList.add('liquid-webgl-target', 'liquid-gpu-target');
      resizeObserver?.observe(node);
    });
    refreshPending = false;
  }

  function targetMaterial(node) {
    if (node.matches('.jty-jelly-target')) return 2;
    if (node.matches('.jty-space-portal, .intimacy-vitals')) return 1;
    return 0;
  }

  function collectTargetRects() {
    return targetNodes.map((node) => {
      if (!visible(node)) return null;
      const rect = node.getBoundingClientRect();
      if (
        rect.width < 2 || rect.height < 2 || rect.right <= 0 || rect.bottom <= 0
        || rect.left >= window.innerWidth || rect.top >= window.innerHeight
      ) return null;
      const clipLeft = Math.max(0, rect.left);
      const clipTop = Math.max(0, rect.top);
      const clipRight = Math.min(window.innerWidth, rect.right);
      const clipBottom = Math.min(window.innerHeight, rect.bottom);
      const style = window.getComputedStyle(node);
      return {
        node,
        x: rect.left,
        y: rect.top,
        width: rect.width,
        height: rect.height,
        clipX: clipLeft,
        clipY: clipTop,
        clipWidth: clipRight - clipLeft,
        clipHeight: clipBottom - clipTop,
        radius: Math.max(0, Number.parseFloat(style.borderTopLeftRadius) || 0),
        material: targetMaterial(node),
      };
    }).filter((rect) => rect && rect.clipWidth > 1 && rect.clipHeight > 1);
  }

  function uniformValues(rect, enabled) {
    const mobile = isMobile();
    const dpr = effectiveDpr;
    const values = new Float32Array(UNIFORM_FLOATS);
    values[0] = canvas.width;
    values[1] = canvas.height;
    values[2] = sceneImage.naturalWidth || sceneImage.width;
    values[3] = sceneImage.naturalHeight || sceneImage.height;
    if (rect && enabled) {
      values[4] = rect.x * dpr;
      values[5] = rect.y * dpr;
      values[6] = rect.width * dpr;
      values[7] = rect.height * dpr;
      values[8] = Math.min(rect.radius, rect.width / 2, rect.height / 2) * dpr;
      values[9] = 1;
      values[10] = rect.material;
      values[12] = pointerX * dpr;
      values[13] = pointerY * dpr;
      values[14] = pointerTarget === rect.node ? 1 : 0;
    }
    values[11] = window.matchMedia('(prefers-reduced-motion: reduce)').matches
      ? 0 : performance.now() / 1000;
    values[15] = (mobile ? 4.4 : 5.2) * dpr;
    values[16] = mobile ? 0.76 : 1;
    values[17] = mobile ? 0.17 : 0.22;
    values[18] = mobile ? 0.22 : 0.30;
    values[19] = mobile ? 0.84 : 1;
    values[20] = -0.68;
    values[21] = -0.73;
    values[22] = mobile ? 0.61 : 0.5;
    values[23] = dpr;
    return values;
  }

  function writeUniform(slot, rect, enabled) {
    const values = uniformValues(rect, enabled);
    device.queue.writeBuffer(
      uniformBuffer,
      slot * uniformStride,
      values.buffer,
      values.byteOffset,
      values.byteLength
    );
  }

  async function draw() {
    raf = 0;
    if (disposed || !api.active) return;
    if (!isRenderActive()) {
      canvas.style.visibility = 'hidden';
      return;
    }
    canvas.style.visibility = 'visible';
    try {
      if (refreshPending) refreshTargets();
      resizeCanvas();
      await ensureTexture();
      if (disposed || !isRenderActive()) return;

      const rects = collectTargetRects();
      const encoder = device.createCommandEncoder({ label: 'JTY liquid glass frame' });
      const pass = encoder.beginRenderPass({
        label: 'JTY liquid glass pass',
        colorAttachments: [{
          view: context.getCurrentTexture().createView(),
          clearValue: { r: 0, g: 0, b: 0, a: 0 },
          loadOp: 'clear',
          storeOp: 'store',
        }],
      });
      pass.setPipeline(pipeline);
      writeUniform(0, null, false);
      pass.setBindGroup(0, bindGroup, [0]);
      pass.setScissorRect(0, 0, canvas.width, canvas.height);
      pass.draw(6);

      rects.forEach((rect, index) => {
        const slot = index + 1;
        writeUniform(slot, rect, true);
        pass.setBindGroup(0, bindGroup, [slot * uniformStride]);
        const x = Math.max(0, Math.floor(rect.clipX * effectiveDpr));
        const y = Math.max(0, Math.floor(rect.clipY * effectiveDpr));
        const width = Math.min(canvas.width - x, Math.max(1, Math.ceil(rect.clipWidth * effectiveDpr)));
        const height = Math.min(canvas.height - y, Math.max(1, Math.ceil(rect.clipHeight * effectiveDpr)));
        pass.setScissorRect(x, y, width, height);
        pass.draw(6);
      });
      pass.end();
      device.queue.submit([encoder.finish()]);

      root.classList.add('webgpu-glass-ready', 'webgl-glass-ready', 'gpu-glass-ready');
      root.dataset.glassRenderer = 'webgpu';
      root.dataset.weatherGlass = 'webgpu';
      root.dataset.weatherRainEngine = 'webgpu-scene';
      if (performance.now() < uiMotionUntil) requestDraw();
    } catch (error) {
      console.warn('[CrystalClear] WebGPU frame failed; switching to WebGL', error);
      fallbackToWebGL('frame-failed');
    }
  }

  function requestDraw() {
    if (disposed || !api.active || raf) return;
    raf = window.requestAnimationFrame(draw);
  }

  function scheduleRefresh() {
    refreshPending = true;
    requestDraw();
  }

  function bindEvents() {
    const refresh = () => {
      sceneKey = '';
      scheduleRefresh();
    };
    window.addEventListener('resize', refresh, { passive: true });
    window.addEventListener('orientationchange', refresh, { passive: true });
    window.visualViewport?.addEventListener('resize', refresh, { passive: true });
    document.addEventListener('scroll', requestDraw, { passive: true, capture: true });
    document.addEventListener('pointermove', (event) => {
      if (event.pointerType === 'touch') return;
      pointerX = event.clientX;
      pointerY = event.clientY;
      pointerTarget = event.target?.closest?.('.liquid-gpu-target') || null;
      requestDraw();
    }, { passive: true });
    window.addEventListener('pointerout', (event) => {
      if (event.relatedTarget) return;
      pointerTarget = null;
      requestDraw();
    }, { passive: true });
    window.addEventListener('blur', () => {
      pointerTarget = null;
      requestDraw();
    });

    [
      'daxigua:weather-frame',
      'daxigua:weather-change',
      'daxigua:weather-blur-change',
    ].forEach((name) => window.addEventListener(name, requestDraw));
    window.addEventListener('daxigua:glass-targets-change', scheduleRefresh);
    mutationObserver = new MutationObserver((records) => {
      if (records.some((record) => record.attributeName === 'data-active-view')) {
        uiMotionUntil = performance.now() + 780;
      }
      // The existing WebGL path owns the animated RainEffect water-normal map.
      // Hand off once when wet weather begins so WebGPU never replaces rain
      // refraction with a static approximation.
      if (root.dataset.weather === 'rain' || root.dataset.weather === 'storm') {
        fallbackToWebGL('rain-watermap-requires-webgl');
        return;
      }
      scheduleRefresh();
    });
    mutationObserver.observe(root, {
      attributes: true,
      attributeFilter: ['data-active-view', 'data-glass-mode', 'data-ui-mode', 'data-weather'],
    });
    domMutationObserver = new MutationObserver((records) => {
      if (!records.some(mutationTouchesGlassTarget)) return;
      scheduleRefresh();
    });
    domMutationObserver.observe(document.body, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ['hidden', 'open', 'aria-hidden'],
    });
    if ('ResizeObserver' in window) {
      resizeObserver = new ResizeObserver(requestDraw);
    }
  }

  function cleanupCanvas() {
    if (raf) window.cancelAnimationFrame(raf);
    raf = 0;
    mutationObserver?.disconnect();
    domMutationObserver?.disconnect();
    resizeObserver?.disconnect();
    targetNodes.forEach((node) => node.classList.remove('liquid-webgl-target', 'liquid-gpu-target'));
    targetNodes = [];
    sceneTexture?.destroy();
    canvas?.remove();
    root.classList.remove('webgpu-glass-ready', 'webgl-glass-ready', 'gpu-glass-ready');
    if (root.dataset.glassRenderer === 'webgpu') delete root.dataset.glassRenderer;
  }

  function fallbackToWebGL(reason) {
    if (disposed) return;
    disposed = true;
    api.active = false;
    api.reason = reason;
    cleanupCanvas();
    root.dataset.weatherGlass = 'none';
    root.dataset.weatherRainEngine = 'none';
    window.JTYLiquidGlassFallbackToWebGL?.();
  }

  async function initialize() {
    if (!window.isSecureContext) {
      api.reason = 'insecure-context';
      return false;
    }
    if (!navigator.gpu) {
      api.reason = 'navigator-gpu-unavailable';
      return false;
    }
    await waitForDom();
    if (root.dataset.weather === 'rain' || root.dataset.weather === 'storm') {
      api.reason = 'rain-watermap-requires-webgl';
      return false;
    }
    adapter = await navigator.gpu.requestAdapter({
      powerPreference: isMobile() ? 'low-power' : 'high-performance',
    });
    if (!adapter) {
      api.reason = 'adapter-unavailable';
      return false;
    }
    device = await adapter.requestDevice({ label: 'JTY liquid glass device' });
    canvas = document.createElement('canvas');
    canvas.id = 'liquid-glass-canvas';
    canvas.setAttribute('aria-hidden', 'true');
    document.body.insertBefore(canvas, document.body.firstChild);
    context = canvas.getContext('webgpu');
    if (!context) throw new Error('WebGPU canvas context unavailable');
    format = navigator.gpu.getPreferredCanvasFormat();
    context.configure({ device, format, alphaMode: 'premultiplied' });

    uniformStride = Math.ceil(
      Math.max(UNIFORM_BYTES, device.limits.minUniformBufferOffsetAlignment) /
      device.limits.minUniformBufferOffsetAlignment
    ) * device.limits.minUniformBufferOffsetAlignment;
    uniformBuffer = device.createBuffer({
      label: 'JTY liquid glass uniforms',
      size: uniformStride * (MAX_TARGETS + 1),
      usage: GPUBufferUsage.UNIFORM | GPUBufferUsage.COPY_DST,
    });
    sampler = device.createSampler({
      label: 'JTY liquid glass sampler',
      addressModeU: 'clamp-to-edge',
      addressModeV: 'clamp-to-edge',
      magFilter: 'linear',
      minFilter: 'linear',
    });
    bindGroupLayout = device.createBindGroupLayout({
      label: 'JTY liquid glass bind group layout',
      entries: [
        {
          binding: 0,
          visibility: GPUShaderStage.FRAGMENT,
          buffer: { type: 'uniform', hasDynamicOffset: true, minBindingSize: UNIFORM_BYTES },
        },
        { binding: 1, visibility: GPUShaderStage.FRAGMENT, sampler: { type: 'filtering' } },
        { binding: 2, visibility: GPUShaderStage.FRAGMENT, texture: { sampleType: 'float' } },
      ],
    });
    const shader = device.createShaderModule({
      label: 'JTY iOS liquid glass WGSL',
      code: SHADER_SOURCE,
    });
    const compilation = await shader.getCompilationInfo();
    const errors = compilation.messages.filter((message) => message.type === 'error');
    if (errors.length) {
      throw new Error(errors.map((message) => message.message).join('\n'));
    }
    pipeline = device.createRenderPipeline({
      label: 'JTY liquid glass pipeline',
      layout: device.createPipelineLayout({ bindGroupLayouts: [bindGroupLayout] }),
      vertex: { module: shader, entryPoint: 'vertexMain' },
      fragment: {
        module: shader,
        entryPoint: 'fragmentMain',
        targets: [{
          format,
          blend: {
            color: { srcFactor: 'src-alpha', dstFactor: 'one-minus-src-alpha', operation: 'add' },
            alpha: { srcFactor: 'one', dstFactor: 'one-minus-src-alpha', operation: 'add' },
          },
        }],
      },
      primitive: { topology: 'triangle-list' },
    });

    api.active = true;
    api.reason = 'ready';
    resizeCanvas();
    refreshTargets();
    bindEvents();
    await ensureTexture();
    requestDraw();
    window.setTimeout(requestDraw, 180);
    window.setTimeout(scheduleRefresh, 520);
    device.lost.then((info) => {
      console.warn('[CrystalClear] WebGPU device lost', info.message || info.reason);
      fallbackToWebGL(`device-lost:${info.reason || 'unknown'}`);
    });
    return true;
  }

  api.ready = initialize().catch((error) => {
    api.reason = `initialization-failed:${error?.message || error}`;
    console.warn('[CrystalClear] WebGPU unavailable; WebGL fallback will start', error);
    cleanupCanvas();
    return false;
  });
})();
