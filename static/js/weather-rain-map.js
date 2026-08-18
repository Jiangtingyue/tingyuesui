/**
 * 大西瓜 · RainEffect 水滴图生成器
 *
 * 按 KiraKiraAyu/RainEffect（e232ebbe）的专用雨滴模型移植：
 * 微滴沉积、主滴下落、拖尾、碰撞合并与路径擦除都写入一张离屏
 * RGBA 水图。R/G 保存折射方向，B 保存滴体深度，A 保存覆盖率；
 * 屏幕上的玻璃 Shader 只读取这张水图，不把 DOM 或文字栅格化。
 */
(function () {
  'use strict';

  const DROP_SIZE = 64;
  const DROP_ALPHA_URL = '/static/images/weather/drop-alpha.png';
  const DROP_COLOR_URL = '/static/images/weather/drop-color.png';

  const DROP_DEFAULTS = Object.freeze({
    x: 0,
    y: 0,
    r: 0,
    spreadX: 0,
    spreadY: 0,
    momentum: 0,
    momentumX: 0,
    lastSpawn: 0,
    nextSpawn: 0,
    parent: null,
    isNew: true,
    killed: false,
    shrink: 0,
  });

  const DEFAULT_OPTIONS = Object.freeze({
    minR: 11,
    maxR: 30,
    maxDrops: 1300,
    rainChance: .16,
    rainLimit: 3,
    dropletsRate: 12,
    dropletsSize: [2.2, 4.4],
    dropletsCleaningRadiusMultiplier: .28,
    raining: true,
    globalTimeScale: 1,
    trailRate: 1,
    autoShrink: true,
    spawnArea: [-.1, .95],
    trailScaleRange: [.25, .35],
    collisionRadius: .45,
    collisionRadiusIncrease: .0002,
    dropFallMultiplier: 1,
    collisionBoostMultiplier: .05,
    collisionBoost: 1,
  });

  function clamp(value, min = 0, max = 1) {
    return Math.max(min, Math.min(max, Number(value) || 0));
  }

  function mix(from, to, amount) {
    return from + (to - from) * amount;
  }

  function random(from = null, to = null, interpolation = null) {
    if (from == null) {
      from = 0;
      to = 1;
    } else if (to == null) {
      to = from;
      from = 0;
    }
    const curve = interpolation || ((number) => number);
    return from + curve(Math.random()) * (to - from);
  }

  function chance(probability) {
    return random() <= probability;
  }

  function createCanvas(width, height) {
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.floor(width));
    canvas.height = Math.max(1, Math.floor(height));
    return canvas;
  }

  function loadImage(url) {
    return new Promise((resolve, reject) => {
      const image = new Image();
      image.decoding = 'async';
      image.addEventListener('load', () => resolve(image), { once: true });
      image.addEventListener(
        'error',
        () => reject(new Error(`无法载入雨滴法线图：${url}`)),
        { once: true }
      );
      image.src = url;
    });
  }

  class RainMap {
    constructor(width, height, scale, dropAlpha, dropColor, options = {}) {
      this.width = width;
      this.height = height;
      this.scale = scale;
      this.dropAlpha = dropAlpha;
      this.dropColor = dropColor;
      this.options = {
        ...DEFAULT_OPTIONS,
        ...options,
        dropletsSize: [...(options.dropletsSize || DEFAULT_OPTIONS.dropletsSize)],
        spawnArea: [...(options.spawnArea || DEFAULT_OPTIONS.spawnArea)],
        trailScaleRange: [
          ...(options.trailScaleRange || DEFAULT_OPTIONS.trailScaleRange),
        ],
      };

      this.canvas = createCanvas(width, height);
      this.context = this.requireContext(this.canvas);
      this.dropletsPixelDensity = 1;
      this.droplets = createCanvas(width, height);
      this.dropletsContext = this.requireContext(this.droplets);
      this.clearDropletsGraphic = createCanvas(128, 128);
      this.dropletsCounter = 0;
      this.drops = [];
      this.dropGraphics = [];
      this.textureCleaningIterations = 0;
      this.lastRender = null;
      this.frameVersion = 0;
      this.targetFrameMs = 1000 / 45;
      this.raf = 0;
      this.destroyed = false;
      this.wasRaining = Boolean(this.options.raining);
      this.visibilityHandler = () => {
        if (!document.hidden) this.wake();
      };
      document.addEventListener('visibilitychange', this.visibilityHandler);

      this.renderDropGraphics();
      this.update();
    }

    get deltaR() {
      return Math.max(.001, this.options.maxR - this.options.minR);
    }

    get area() {
      return (this.width * this.height) / Math.max(this.scale, .25);
    }

    get areaMultiplier() {
      return Math.sqrt(this.area / (1024 * 768));
    }

    requireContext(canvas) {
      const context = canvas.getContext('2d', { alpha: true });
      if (!context) throw new Error('2D canvas context is unavailable');
      return context;
    }

    resize(width, height, scale) {
      const nextWidth = Math.max(1, Math.floor(width));
      const nextHeight = Math.max(1, Math.floor(height));
      if (
        nextWidth === this.canvas.width
        && nextHeight === this.canvas.height
        && Math.abs(scale - this.scale) < .001
      ) return;

      this.width = nextWidth;
      this.height = nextHeight;
      this.scale = scale;
      this.canvas.width = nextWidth;
      this.canvas.height = nextHeight;
      this.droplets.width = nextWidth;
      this.droplets.height = nextHeight;
      this.drops = [];
      this.dropletsCounter = 0;
      this.lastRender = null;
      this.frameVersion += 1;
      this.wake();
    }

    setWeather(weather) {
      const precipitation = clamp(weather?.precipitation);
      const intensity = precipitation ** 1.35;
      const density = .75 + .25 * intensity;
      const wetness = clamp(weather?.glassWetness);
      const flow = clamp(weather?.glassFlow);
      const raining = precipitation > .025 && wetness > .08;
      const viewportArea = Math.max(1, (this.width / this.scale) * (this.height / this.scale));
      const targetFps = viewportArea >= 5_000_000 ? 30
        : viewportArea >= 3_000_000 ? 36
          : 45;
      this.targetFrameMs = 1000 / targetFps;

      this.options.raining = raining;
      // Keep the original drop size and motion. Only density follows the
      // weather strength, so persistent rain stays light while storm stays
      // exactly at the original full-rain amount.
      const moodTempoRaw = parseFloat(
        getComputedStyle(document.documentElement).getPropertyValue('--jty-rain-tempo') || '1'
      );
      const moodTempo = Number.isFinite(moodTempoRaw)
        ? Math.max(.92, Math.min(1.10, moodTempoRaw))
        : 1;
      this.options.globalTimeScale = moodTempo;
      this.options.trailRate = 1;
      this.options.trailScaleRange = [.25, .35];
      this.options.collisionRadius = .45;
      this.options.dropletsCleaningRadiusMultiplier = .28;
      this.options.minR = 20;
      this.options.maxR = 50;
      this.options.rainChance = .35 * density;
      this.options.rainLimit = 6;
      this.options.dropletsRate = 50 * density;
      this.options.dropletsSize = [3, 5.5];
      this.options.collisionRadiusIncrease = .0002;

      if (this.wasRaining && !raining) this.clearDrops();
      if (!this.wasRaining && raining) this.primeRain(intensity);
      this.wasRaining = raining;
      if (raining) this.wake();
    }

    primeRain() {
      // Upstream RainEffect does not pre-seed an artificial dirty layer.
      // Start from a clean pane and let its own rain loop create the drops.
      this.dropletsContext.clearRect(0, 0, this.droplets.width, this.droplets.height);
      this.drops = [];
      this.dropletsCounter = 0;
    }

    createDrop(options) {
      if (this.drops.length >= this.options.maxDrops * this.areaMultiplier) {
        return null;
      }
      return { ...DROP_DEFAULTS, ...options };
    }

    clearDrops() {
      this.drops.forEach((drop) => {
        drop.shrink = .13 + random(.42);
      });
      this.textureCleaningIterations = 50;
      this.wake();
    }

    destroy() {
      this.destroyed = true;
      if (this.raf) cancelAnimationFrame(this.raf);
      this.raf = 0;
      document.removeEventListener('visibilitychange', this.visibilityHandler);
    }

    wake() {
      if (this.destroyed || this.raf || document.hidden) return;
      this.raf = requestAnimationFrame(this.update);
    }

    renderDropGraphics() {
      const dropBuffer = createCanvas(DROP_SIZE, DROP_SIZE);
      const dropBufferContext = this.requireContext(dropBuffer);

      this.dropGraphics = Array.from({ length: 255 }, (_, index) => {
        const drop = createCanvas(DROP_SIZE, DROP_SIZE);
        const dropContext = this.requireContext(drop);

        dropBufferContext.clearRect(0, 0, DROP_SIZE, DROP_SIZE);
        dropBufferContext.globalCompositeOperation = 'source-over';
        dropBufferContext.drawImage(
          this.dropColor,
          0,
          0,
          DROP_SIZE,
          DROP_SIZE
        );
        dropBufferContext.globalCompositeOperation = 'screen';
        dropBufferContext.fillStyle = `rgba(0,0,${index},1)`;
        dropBufferContext.fillRect(0, 0, DROP_SIZE, DROP_SIZE);

        dropContext.globalCompositeOperation = 'source-over';
        dropContext.drawImage(
          this.dropAlpha,
          0,
          0,
          DROP_SIZE,
          DROP_SIZE
        );
        dropContext.globalCompositeOperation = 'source-in';
        dropContext.drawImage(dropBuffer, 0, 0, DROP_SIZE, DROP_SIZE);
        return drop;
      });

      const clearContext = this.requireContext(this.clearDropletsGraphic);
      clearContext.fillStyle = '#000';
      clearContext.beginPath();
      clearContext.arc(64, 64, 64, 0, Math.PI * 2);
      clearContext.fill();
    }

    drawDroplet(x, y, radius) {
      this.drawDrop(this.dropletsContext, {
        ...DROP_DEFAULTS,
        x: x * this.dropletsPixelDensity,
        y: y * this.dropletsPixelDensity,
        r: radius * this.dropletsPixelDensity,
      });
    }

    drawDrop(context, drop) {
      const { x, y, r, spreadX, spreadY } = drop;
      const scaleX = 1;
      const scaleY = 1.5;
      let depth = clamp(((r - this.options.minR) / this.deltaR) * .9);
      depth *= 1 / ((spreadX + spreadY) * .5 + 1);
      const graphicIndex = Math.floor(depth * (this.dropGraphics.length - 1));

      context.globalAlpha = 1;
      context.globalCompositeOperation = 'source-over';
      context.drawImage(
        this.dropGraphics[graphicIndex],
        (x - r * scaleX * (spreadX + 1)) * this.scale,
        (y - r * scaleY * (spreadY + 1)) * this.scale,
        r * 2 * scaleX * (spreadX + 1) * this.scale,
        r * 2 * scaleY * (spreadY + 1) * this.scale
      );
    }

    clearDroplets(x, y, radius = 30) {
      this.dropletsContext.globalCompositeOperation = 'destination-out';
      this.dropletsContext.drawImage(
        this.clearDropletsGraphic,
        (x - radius) * this.dropletsPixelDensity * this.scale,
        (y - radius) * this.dropletsPixelDensity * this.scale,
        radius * 2 * this.dropletsPixelDensity * this.scale,
        radius * 3 * this.dropletsPixelDensity * this.scale
      );
    }

    updateRain(timeScale) {
      const rainDrops = [];
      if (!this.options.raining) return rainDrops;

      const limit = this.options.rainLimit * timeScale * this.areaMultiplier;
      let count = 0;
      while (
        chance(this.options.rainChance * timeScale * this.areaMultiplier)
        && count < limit
      ) {
        count += 1;
        const radius = random(
          this.options.minR,
          this.options.maxR,
          (number) => number ** 3
        );
        const rainDrop = this.createDrop({
          x: random(this.width / this.scale),
          y: random(
            (this.height / this.scale) * this.options.spawnArea[0],
            (this.height / this.scale) * this.options.spawnArea[1]
          ),
          r: radius,
          momentum: 1 + (radius - this.options.minR) * .1 + random(2),
          spreadX: 1.5,
          spreadY: 1.5,
        });
        if (rainDrop) rainDrops.push(rainDrop);
      }
      return rainDrops;
    }

    updateDroplets(timeScale) {
      if (this.textureCleaningIterations > 0) {
        this.textureCleaningIterations -= timeScale;
        this.dropletsContext.globalCompositeOperation = 'destination-out';
        this.dropletsContext.fillStyle = `rgba(0,0,0,${.05 * timeScale})`;
        this.dropletsContext.fillRect(0, 0, this.width, this.height);
      }

      if (this.options.raining) {
        this.dropletsCounter += (
          this.options.dropletsRate * timeScale * this.areaMultiplier
        );
        while (this.dropletsCounter >= 1) {
          this.dropletsCounter -= 1;
          this.drawDroplet(
            random(this.width / this.scale),
            random(this.height / this.scale),
            random(
              this.options.dropletsSize[0],
              this.options.dropletsSize[1],
              (number) => number * number
            )
          );
        }
      }
      this.context.drawImage(this.droplets, 0, 0, this.width, this.height);
    }

    updateDrops(timeScale) {
      let newDrops = [];
      this.updateDroplets(timeScale);
      newDrops = newDrops.concat(this.updateRain(timeScale));

      this.drops.sort((first, second) => {
        const firstValue = first.y * (this.width / this.scale) + first.x;
        const secondValue = second.y * (this.width / this.scale) + second.x;
        return firstValue - secondValue;
      });

      this.drops.forEach((drop, index) => {
        if (drop.killed) return;

        if (chance(
          (drop.r - this.options.minR * this.options.dropFallMultiplier)
          * (.1 / this.deltaR)
          * timeScale
        )) {
          drop.momentum += random((drop.r / this.options.maxR) * 4);
        }

        if (
          this.options.autoShrink
          && drop.r <= this.options.minR
          && chance(.05 * timeScale)
        ) {
          drop.shrink += .01;
        }

        drop.r -= drop.shrink * timeScale;
        if (drop.r <= 0) drop.killed = true;

        if (this.options.raining) {
          drop.lastSpawn += drop.momentum * timeScale * this.options.trailRate;
          if (drop.lastSpawn > drop.nextSpawn) {
            const trailDrop = this.createDrop({
              x: drop.x + random(-drop.r, drop.r) * .1,
              y: drop.y - drop.r * .01,
              r: drop.r * random(
                this.options.trailScaleRange[0],
                this.options.trailScaleRange[1]
              ),
              spreadY: drop.momentum * .1,
              parent: drop,
            });
            if (trailDrop) {
              newDrops.push(trailDrop);
              drop.r *= .97 ** timeScale;
              drop.lastSpawn = 0;
              drop.nextSpawn = random(this.options.minR, this.options.maxR)
                - drop.momentum * 2 * this.options.trailRate
                + (this.options.maxR - drop.r);
            }
          }
        }

        drop.spreadX *= .4 ** timeScale;
        drop.spreadY *= .7 ** timeScale;
        const moved = drop.momentum > 0;
        if (moved && !drop.killed) {
          drop.y += drop.momentum * timeScale;
          drop.x += drop.momentumX * timeScale;
          if (drop.y > this.height / this.scale + drop.r) drop.killed = true;
        }

        const shouldCheckCollision = (moved || drop.isNew) && !drop.killed;
        drop.isNew = false;
        if (shouldCheckCollision) {
          this.drops.slice(index + 1, index + 70).forEach((other) => {
            if (
              drop === other
              || drop.r <= other.r
              || drop.parent === other
              || other.parent === drop
              || other.killed
            ) return;

            const deltaX = other.x - drop.x;
            const deltaY = other.y - drop.y;
            const distance = Math.sqrt(deltaX * deltaX + deltaY * deltaY);
            const collisionRadius = (
              this.options.collisionRadius
              + drop.momentum
                * this.options.collisionRadiusIncrease
                * timeScale
            );
            if (distance >= (drop.r + other.r) * collisionRadius) return;

            const firstArea = Math.PI * drop.r * drop.r;
            const secondArea = Math.PI * other.r * other.r;
            const targetRadius = Math.sqrt(
              (firstArea + secondArea * .8) / Math.PI
            );
            drop.r = targetRadius;
            drop.momentumX += deltaX * .1;
            drop.spreadX = 0;
            drop.spreadY = 0;
            other.killed = true;
            drop.momentum = Math.max(
              other.momentum,
              Math.min(
                40,
                drop.momentum
                  + targetRadius * this.options.collisionBoostMultiplier
                  + this.options.collisionBoost
              )
            );
          });
        }

        drop.momentum -= Math.max(
          1,
          this.options.minR * .5 - drop.momentum
        ) * .1 * timeScale;
        if (drop.momentum < 0) drop.momentum = 0;
        drop.momentumX *= .7 ** timeScale;

        if (!drop.killed) {
          newDrops.push(drop);
          if (moved && this.options.dropletsRate > 0) {
            this.clearDroplets(
              drop.x,
              drop.y,
              drop.r * this.options.dropletsCleaningRadiusMultiplier
            );
          }
          this.drawDrop(this.context, drop);
        }
      });
      this.drops = newDrops;
    }

    update = () => {
      this.raf = 0;
      if (this.destroyed) return;
      if (document.hidden) {
        this.lastRender = null;
        return;
      }

      const now = performance.now();
      if (this.lastRender == null) this.lastRender = now - this.targetFrameMs;
      const deltaTime = now - this.lastRender;
      if (deltaTime + 0.5 < this.targetFrameMs) {
        this.raf = requestAnimationFrame(this.update);
        return;
      }
      let timeScale = deltaTime / ((1 / 60) * 1000);
      // Preserve physical speed when frames are intentionally coalesced, while
      // still bounding giant jumps after a busy tab or resize.
      if (timeScale > 2.2) timeScale = 2.2;
      timeScale *= this.options.globalTimeScale;
      this.lastRender = now;

      this.context.clearRect(0, 0, this.width, this.height);
      this.updateDrops(timeScale);
      this.frameVersion += 1;
      if (
        this.options.raining
        || this.drops.length > 0
        || this.textureCleaningIterations > 0
      ) {
        this.raf = requestAnimationFrame(this.update);
      } else {
        this.lastRender = null;
      }
    };
  }

  async function create(options = {}) {
    const [dropAlpha, dropColor] = await Promise.all([
      loadImage(DROP_ALPHA_URL),
      loadImage(DROP_COLOR_URL),
    ]);
    return new RainMap(
      options.width || window.innerWidth,
      options.height || window.innerHeight,
      options.scale || 1,
      dropAlpha,
      dropColor,
      { ...options, raining: options.raining ?? false }
    );
  }

  window.DaxiguaRainMap = Object.freeze({ create });
})();
