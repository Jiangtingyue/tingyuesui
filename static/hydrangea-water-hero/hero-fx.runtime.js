(function () {
  'use strict';

  const PETAL_COUNT = 5;
  const SPARKLE_COUNT = 18;
  const PETAL_PALETTE = [
    '#bce8de', '#e6f8f2', '#9fd8cb',
    '#def0e2', '#f4faf1', '#c2e4cc',
    '#f6cfde', '#fde9f0', '#eeb3ca',
    '#f8dce6', '#fdf0f5', '#f0c2d2',
  ];

  function clamp(value, min, max) { return Math.max(min, Math.min(max, value)); }

  function createPetalSprite(color) {
    const size = 72;
    const sprite = document.createElement('canvas');
    sprite.width = size;
    sprite.height = size;
    const ctx = sprite.getContext('2d');
    if (!ctx) return sprite;
    ctx.translate(size / 2, size / 2);
    ctx.rotate(-0.18);
    const gradient = ctx.createRadialGradient(-8, -12, 2, 0, 0, 30);
    gradient.addColorStop(0, 'rgba(255,255,255,0.96)');
    gradient.addColorStop(0.22, color);
    gradient.addColorStop(1, color);
    ctx.fillStyle = gradient;
    ctx.beginPath();
    ctx.moveTo(0, -28);
    ctx.bezierCurveTo(19, -20, 25, 2, 9, 24);
    ctx.bezierCurveTo(3, 32, -3, 32, -9, 24);
    ctx.bezierCurveTo(-25, 2, -19, -20, 0, -28);
    ctx.closePath();
    ctx.fill();
    ctx.strokeStyle = 'rgba(255,255,255,0.34)';
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.moveTo(0, -21);
    ctx.quadraticCurveTo(2, -2, 0, 24);
    ctx.stroke();
    return sprite;
  }

  function randomPetal(width, height, index) {
    return {
      x: width * (0.12 + ((index * 0.19 + Math.random() * 0.08) % 0.82)),
      y: height * Math.random(),
      vx: (Math.random() - 0.5) * 0.08,
      vy: 0.055 + Math.random() * 0.075,
      rotation: Math.random() * Math.PI * 2,
      spin: (Math.random() - 0.5) * 0.0014,
      scale: 0.42 + Math.random() * 0.36,
      drift: Math.random() * Math.PI * 2,
      spriteIndex: index % PETAL_PALETTE.length,
    };
  }

  function randomSparkle(width, height) {
    return {
      x: Math.random() * width,
      y: Math.random() * height,
      radius: 0.7 + Math.random() * 1.8,
      alpha: 0.18 + Math.random() * 0.62,
      phase: Math.random() * Math.PI * 2,
      speed: 0.0012 + Math.random() * 0.0028,
      drift: (Math.random() - 0.5) * 0.018,
    };
  }

  function initHeroFx(options) {
    options = options || {};
    const canvas = options.canvas;
    const gradAt = options.gradAt || function () { return { x: 0, y: 0 }; };
    const isActive = options.isActive || function () { return true; };
    if (!canvas) return null;
    const ctx = canvas.getContext('2d', { alpha: true });
    if (!ctx) return null;

    const sprites = PETAL_PALETTE.map(createPetalSprite);
    let width = 1;
    let height = 1;
    let dpr = 1;
    let petals = [];
    let sparkles = [];
    let raf = 0;
    let running = false;
    let ambientEnabled = options.ambientEnabled !== false;
    let lastTime = performance.now();

    function resize(force) {
      const nextWidth = Math.max(1, window.innerWidth);
      const nextHeight = Math.max(1, window.innerHeight);
      const nextDpr = Math.min(window.devicePixelRatio || 1, 2);
      const pixelWidth = Math.max(1, Math.round(nextWidth * nextDpr));
      const pixelHeight = Math.max(1, Math.round(nextHeight * nextDpr));
      const changed = !!force || canvas.width !== pixelWidth || canvas.height !== pixelHeight;
      width = nextWidth;
      height = nextHeight;
      dpr = nextDpr;
      if (!changed) return;
      canvas.width = pixelWidth;
      canvas.height = pixelHeight;
      canvas.style.width = width + 'px';
      canvas.style.height = height + 'px';
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      if (!petals.length) petals = Array.from({ length: PETAL_COUNT }, function (_, i) { return randomPetal(width, height, i); });
      if (!sparkles.length) sparkles = Array.from({ length: SPARKLE_COUNT }, function () { return randomSparkle(width, height); });
    }

    function drawPetals(now, dt) {
      ctx.globalCompositeOperation = 'source-over';
      petals.forEach(function (petal) {
        const grad = gradAt(petal.x, petal.y) || { x: 0, y: 0 };
        const gx = Number.isFinite(grad.x) ? grad.x : 0;
        const gy = Number.isFinite(grad.y) ? grad.y : 0;
        petal.drift += dt * 0.00022;
        petal.vx += clamp(gx * 0.045, -0.016, 0.016);
        petal.vy += clamp(-gy * 0.03, -0.012, 0.012);
        petal.vx *= 0.987;
        petal.vy = petal.vy * 0.992 + 0.00035 * dt;
        petal.x += (petal.vx + Math.sin(petal.drift + now * 0.00018) * 0.025) * dt;
        petal.y += petal.vy * dt;
        petal.rotation += (petal.spin + gx * 0.00008) * dt;
        if (petal.y > height + 54) {
          petal.y = -54;
          petal.x = Math.random() * width;
          petal.vy = 0.055 + Math.random() * 0.075;
        }
        if (petal.x < -64) petal.x = width + 64;
        if (petal.x > width + 64) petal.x = -64;
        const sprite = sprites[petal.spriteIndex];
        const drawSize = 44 * petal.scale;
        ctx.save();
        ctx.translate(petal.x, petal.y);
        ctx.rotate(petal.rotation);
        ctx.globalAlpha = 0.72;
        ctx.drawImage(sprite, -drawSize / 2, -drawSize / 2, drawSize, drawSize);
        ctx.restore();
      });
    }

    function drawSparkles(now, dt) {
      ctx.save();
      ctx.globalCompositeOperation = 'lighter';
      sparkles.forEach(function (sparkle) {
        sparkle.y += sparkle.drift * dt;
        if (sparkle.y < -8) sparkle.y = height + 8;
        if (sparkle.y > height + 8) sparkle.y = -8;
        const pulse = 0.5 + 0.5 * Math.sin(sparkle.phase + now * sparkle.speed);
        const alpha = sparkle.alpha * (0.25 + pulse * 0.75);
        const radius = sparkle.radius * (0.75 + pulse * 0.55);
        const glow = ctx.createRadialGradient(sparkle.x, sparkle.y, 0, sparkle.x, sparkle.y, radius * 5);
        glow.addColorStop(0, 'rgba(255,255,248,' + alpha + ')');
        glow.addColorStop(0.22, 'rgba(224,250,244,' + (alpha * 0.72) + ')');
        glow.addColorStop(1, 'rgba(224,250,244,0)');
        ctx.fillStyle = glow;
        ctx.beginPath();
        ctx.arc(sparkle.x, sparkle.y, radius * 5, 0, Math.PI * 2);
        ctx.fill();
      });
      ctx.restore();
    }

    function frame(now) {
      raf = 0;
      if (!running) return;
      if (document.hidden || !isActive()) return;
      resize(false);
      const dt = Math.min(34, Math.max(8, now - lastTime));
      lastTime = now;
      ctx.clearRect(0, 0, width, height);
      drawPetals(now, dt);
      drawSparkles(now, dt);
      raf = requestAnimationFrame(frame);
    }

    function start() {
      if (running || !ambientEnabled) return;
      running = true;
      lastTime = performance.now();
      resize(true);
      if (!raf) raf = requestAnimationFrame(frame);
    }

    function stop() {
      running = false;
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      ctx.clearRect(0, 0, width, height);
    }

    function applyConfig(next) {
      const shouldEnable = !next || next.ambient_fx !== false;
      if (shouldEnable === ambientEnabled) return;
      ambientEnabled = shouldEnable;
      if (!ambientEnabled) stop();
      else if (isActive() && !document.hidden) start();
    }

    function onVisibilityChange() {
      if (document.hidden) {
        if (raf) cancelAnimationFrame(raf);
        raf = 0;
      } else if (running && isActive() && !raf) {
        lastTime = performance.now();
        raf = requestAnimationFrame(frame);
      }
    }

    window.addEventListener('resize', resize, { passive: true });
    document.addEventListener('visibilitychange', onVisibilityChange);
    return {
      start: start,
      stop: stop,
      resize: resize,
      applyConfig: applyConfig,
      destroy: function () {
        stop();
        window.removeEventListener('resize', resize);
        document.removeEventListener('visibilitychange', onVisibilityChange);
      },
    };
  }

  window.JTYHydrangeaHeroFx = {
    initHeroFx: initHeroFx,
    PETAL_COUNT: PETAL_COUNT,
    SPARKLE_COUNT: SPARKLE_COUNT,
    PETAL_PALETTE: PETAL_PALETTE,
  };
})();
