/**
 * 大西瓜 · Crystal Clear + Ambient Weather WebGL
 *
 * 一个固定 Canvas 在 DOM 背后绘制完整的湿窗：背景与水图都使用统一的
 * 屏幕坐标，水迹不会在卡片边缘中断。DOM 继续负责文字和交互；Canvas
 * 只采样背景与离屏 RGBA 水图，不栅格化任何界面内容。
 */
(function () {
  'use strict';

  const MOBILE_BREAKPOINT = 768;
  const DPR_LIMIT = 1.15;
  const MAX_BACKBUFFER_PIXELS = 1_300_000;
  const DESKTOP_TARGETS = [
    '#view-home .home-hero .hero-copy',
    '.sidebar-companion-card',
    '.dwell-life-hero',
    '.intimacy-vitals:not([hidden])',
    '.v872-glass-target',
    '.jty-space-glass',
    '.jty-jelly-target',
    '.jty-3d-glass-target',
  ];
  const MOBILE_TARGETS = DESKTOP_TARGETS;

  const ALL_TARGET_QUERY = Array.from(new Set(DESKTOP_TARGETS)).join(',');

  const VERTEX_SOURCE = `
    attribute vec2 a_position;
    varying vec2 v_uv;

    void main() {
      v_uv = a_position * 0.5 + 0.5;
      gl_Position = vec4(a_position, 0.0, 1.0);
    }
  `;

  const FRAGMENT_SOURCE = `
    precision highp float;

    varying vec2 v_uv;
    uniform sampler2D u_scene;
    uniform sampler2D u_sceneRainFg;
    uniform sampler2D u_sceneRainBg;
    uniform sampler2D u_waterMap;
    uniform vec2 u_viewport;
    uniform vec2 u_imageSize;
    uniform vec2 u_bgPosition;
    uniform vec2 u_rainImageSize;
    uniform vec2 u_rainBgPosition;
    uniform float u_sceneBlurPx;
    uniform vec4 u_weatherSurface;
    uniform vec4 u_weatherColor;
    uniform vec4 u_weatherOptics;
    uniform vec4 u_weatherAtmosphere;
    uniform vec4 u_targetRect;
    uniform float u_targetRadius;
    uniform float u_targetEnabled;
    uniform float u_targetMaterial;
    uniform vec2 u_lightDirection;
    uniform vec4 u_materialParams;
    uniform vec3 u_pointer;

    vec2 roundedRectNormal(
      vec2 point, vec2 center, vec2 halfSize, float radius
    ) {
      vec2 local = point - center;
      vec2 inner = max(halfSize - vec2(radius), vec2(1.0));
      vec2 corner = local - clamp(local, -inner, inner);
      if (length(corner) > 0.001) return normalize(corner);
      vec2 edgeDistance = halfSize - abs(local);
      return edgeDistance.x < edgeDistance.y
        ? vec2(sign(local.x), 0.0)
        : vec2(0.0, sign(local.y));
    }

    float roundedRectDepth(
      vec2 point, vec2 center, vec2 halfSize, float radius
    ) {
      vec2 q = abs(point - center) - max(halfSize - vec2(radius), vec2(0.0));
      float outside = length(max(q, vec2(0.0))) - radius;
      float inside = min(max(q.x, q.y), 0.0);
      return max(-(outside + inside), 0.0);
    }

    vec2 coverUV(
      vec2 screenPx, vec2 imageSize, vec2 bgPosition
    ) {
      float scale = max(
        u_viewport.x / imageSize.x,
        u_viewport.y / imageSize.y
      );
      vec2 rendered = imageSize * scale;
      vec2 offset = (u_viewport - rendered) * bgPosition;
      return (screenPx - offset) / rendered;
    }

    vec2 sceneUV(vec2 screenPx) {
      return coverUV(screenPx, u_imageSize, u_bgPosition);
    }

    vec2 rainSceneUV(vec2 screenPx) {
      return coverUV(screenPx, u_rainImageSize, u_rainBgPosition);
    }

    vec3 weatherTone(vec3 color, vec2 screenPx) {
      float luminance = dot(color, vec3(0.2126, 0.7152, 0.0722));
      color = mix(vec3(luminance), color, u_weatherColor.y);
      color = (color - 0.5) * u_weatherOptics.y + 0.5;
      color *= u_weatherColor.x;
      float cool = max(-u_weatherColor.z, 0.0);
      float warm = max(u_weatherColor.z, 0.0);
      color *= mix(vec3(1.0), vec3(0.87, 0.95, 1.08), cool * 0.38);
      color *= mix(vec3(1.0), vec3(1.07, 0.98, 0.88), warm * 0.25);
      color = mix(color, vec3(0.76, 0.84, 0.85), u_weatherColor.w * 0.12);
      vec2 screenUv = screenPx / max(u_viewport, vec2(1.0));
      float skyRegion = 1.0 - smoothstep(0.24, 0.72, screenUv.y);
      float cloudShade = u_weatherAtmosphere.x
        * (0.09 + u_weatherAtmosphere.y * 0.17)
        * skyRegion;
      color = mix(color, vec3(0.34, 0.43, 0.48), cloudShade);
      float lowFog = smoothstep(0.46, 1.0, screenUv.y)
        * u_weatherAtmosphere.z;
      color = mix(color, vec3(0.78, 0.87, 0.87), lowFog * 0.17);
      float roadLight = smoothstep(0.62, 1.0, screenUv.y)
        * u_weatherAtmosphere.w;
      color += roadLight * vec3(0.035, 0.052, 0.056);
      return color;
    }

    vec3 sampleClearTexture(vec2 screenPx) {
      vec2 uv = clamp(sceneUV(screenPx), vec2(0.001), vec2(0.999));
      vec3 center = texture2D(u_scene, uv).rgb;
      if (u_sceneBlurPx < 0.05) return center;
      vec2 diagonal = vec2(u_sceneBlurPx * 0.7071);
      vec3 color = center * 0.36;
      color += texture2D(
        u_scene,
        clamp(sceneUV(screenPx + vec2(diagonal.x, diagonal.y)), vec2(0.001), vec2(0.999))
      ).rgb * 0.16;
      color += texture2D(
        u_scene,
        clamp(sceneUV(screenPx + vec2(-diagonal.x, diagonal.y)), vec2(0.001), vec2(0.999))
      ).rgb * 0.16;
      color += texture2D(
        u_scene,
        clamp(sceneUV(screenPx + vec2(diagonal.x, -diagonal.y)), vec2(0.001), vec2(0.999))
      ).rgb * 0.16;
      color += texture2D(
        u_scene,
        clamp(sceneUV(screenPx - diagonal), vec2(0.001), vec2(0.999))
      ).rgb * 0.16;
      return color;
    }

    vec3 sampleRainBackgroundTexture(vec2 screenPx) {
      vec2 uv = clamp(rainSceneUV(screenPx), vec2(0.001), vec2(0.999));
      vec3 center = texture2D(u_sceneRainBg, uv).rgb;
      if (u_sceneBlurPx < 0.05) return center;
      vec2 diagonal = vec2(u_sceneBlurPx * 0.7071);
      vec3 color = center * 0.36;
      color += texture2D(u_sceneRainBg, clamp(rainSceneUV(screenPx + diagonal), vec2(0.001), vec2(0.999))).rgb * 0.16;
      color += texture2D(u_sceneRainBg, clamp(rainSceneUV(screenPx + vec2(-diagonal.x, diagonal.y)), vec2(0.001), vec2(0.999))).rgb * 0.16;
      color += texture2D(u_sceneRainBg, clamp(rainSceneUV(screenPx + vec2(diagonal.x, -diagonal.y)), vec2(0.001), vec2(0.999))).rgb * 0.16;
      color += texture2D(u_sceneRainBg, clamp(rainSceneUV(screenPx - diagonal), vec2(0.001), vec2(0.999))).rgb * 0.16;
      return color;
    }

    vec3 sampleRainForegroundTexture(vec2 screenPx) {
      vec2 uv = clamp(rainSceneUV(screenPx), vec2(0.001), vec2(0.999));
      vec3 center = texture2D(u_sceneRainFg, uv).rgb;
      if (u_sceneBlurPx < 0.05) return center;
      vec2 diagonal = vec2(u_sceneBlurPx * 0.7071);
      vec3 color = center * 0.36;
      color += texture2D(u_sceneRainFg, clamp(rainSceneUV(screenPx + diagonal), vec2(0.001), vec2(0.999))).rgb * 0.16;
      color += texture2D(u_sceneRainFg, clamp(rainSceneUV(screenPx + vec2(-diagonal.x, diagonal.y)), vec2(0.001), vec2(0.999))).rgb * 0.16;
      color += texture2D(u_sceneRainFg, clamp(rainSceneUV(screenPx + vec2(diagonal.x, -diagonal.y)), vec2(0.001), vec2(0.999))).rgb * 0.16;
      color += texture2D(u_sceneRainFg, clamp(rainSceneUV(screenPx - diagonal), vec2(0.001), vec2(0.999))).rgb * 0.16;
      return color;
    }

    vec3 sampleScene(vec2 screenPx) {
      vec3 color = sampleClearTexture(screenPx);
      // In rain, preserve the original hydrangea photograph exactly.
      if (u_weatherSurface.z > 0.02) return color;
      return weatherTone(color, screenPx);
    }

    vec3 sampleRainBackground(vec2 screenPx) {
      return weatherTone(sampleRainBackgroundTexture(screenPx), screenPx);
    }

    vec3 sampleRainForeground(vec2 screenPx) {
      // RainEffect is applied over our untouched original hydrangea source.
      // No pre-baked haze, dust, cool tint or weather grading is sampled here.
      return sampleRainForegroundTexture(screenPx);
    }

    vec4 sampleWater(vec2 screenPx, vec2 offsetPx) {
      vec2 uv = clamp(
        (screenPx + offsetPx) / max(u_viewport, vec2(1.0)),
        vec2(0.001),
        vec2(0.999)
      );
      return texture2D(u_waterMap, uv);
    }

    float skyFlash(float time) {
      float cycle = fract(time / 11.5);
      float first = smoothstep(0.865, 0.872, cycle)
        * (1.0 - smoothstep(0.879, 0.888, cycle));
      float second = smoothstep(0.895, 0.901, cycle)
        * (1.0 - smoothstep(0.91, 0.922, cycle));
      return max(first, second * 0.68);
    }

    void main() {
      vec2 screenPx = vec2(
        v_uv.x * u_viewport.x,
        (1.0 - v_uv.y) * u_viewport.y
      );
      vec2 targetCenter = u_targetRect.xy + u_targetRect.zw * 0.5;
      vec2 targetHalf = max(u_targetRect.zw * 0.5, vec2(1.0));
      vec2 relative = (screenPx - targetCenter) / targetHalf;
      float depth = roundedRectDepth(
        screenPx, targetCenter, targetHalf, u_targetRadius
      );
      vec2 normal = roundedRectNormal(
        screenPx, targetCenter, targetHalf, u_targetRadius
      );
      /* Narrower rims keep the glass readable.  The old 48–74px falloff
         dissolved the boundary into the scene and made panels hard to locate. */
      float standardRim = 1.0 - smoothstep(0.8, 38.0, depth);
      float iosRim = 1.0 - smoothstep(0.8, 30.0, depth);
      float jellyRim = 1.0 - smoothstep(0.8, 46.0, depth);
      float iosMaterial = step(0.5, u_targetMaterial) * (1.0 - step(1.5, u_targetMaterial));
      float jellyMaterial = step(1.5, u_targetMaterial);
      vec2 sharedLightDir = normalize(u_lightDirection);
      float refractionScale = u_materialParams.x;
      float fresnelStrength = u_materialParams.y;
      float reflectionStrength = u_materialParams.z;
      float mobileMaterialFactor = u_materialParams.w;
      float rim = mix(standardRim, iosRim, iosMaterial);
      rim = mix(rim, jellyRim, jellyMaterial);
      rim *= u_targetEnabled;
      float crystalEdge = smoothstep(0.12, 0.92, rim) * u_targetEnabled;
      float pointerDistance = distance(screenPx, u_pointer.xy);
      float pointerLift = u_pointer.z
        * (1.0 - smoothstep(22.0, 180.0, pointerDistance));
      float edgeRefraction = mix(
        2.2 + crystalEdge * 2.8,
        7.4 + crystalEdge * 14.6,
        iosMaterial
      );
      edgeRefraction = mix(edgeRefraction, 5.2 + crystalEdge * 12.4, jellyMaterial) * refractionScale;
      float jellyWaveA = sin(relative.y * 5.7 + u_weatherOptics.w * 0.17);
      float jellyWaveB = cos(relative.x * 4.1 - u_weatherOptics.w * 0.13);
      vec2 jellyFlow = vec2(jellyWaveA, jellyWaveB)
        * jellyMaterial * (0.72 + rim * 1.52);
      vec2 samplePx = screenPx + normal
        * rim * (edgeRefraction + pointerLift * mix(2.2, 3.6, iosMaterial) * mobileMaterialFactor)
        + jellyFlow;

      float localWetness = u_weatherSurface.x;
      float rainBlend = smoothstep(0.08, 0.86, localWetness)
        * smoothstep(0.015, 0.22, u_weatherSurface.z);
      float dropDepth = 0.0;
      float dropAlpha = 0.0;
      vec2 dropRefraction = vec2(0.0);
      float dropSpecular = 0.0;

      /* 中央零色散，边缘最多不到一个 CSS 像素。 */
      float chroma = rim * mix(
        0.12 + crystalEdge * 0.46,
        0.34 + crystalEdge * 0.92,
        iosMaterial
      );
      chroma *= mix(1.0, 0.18, jellyMaterial);
      vec2 chromaShift = normal * chroma;
      vec3 dryRefracted;
      if (u_sceneBlurPx > 0.05) {
        // The blur already removes sub-pixel colour separation. One five-tap
        // sample keeps the adjustable high end usable on integrated GPUs.
        dryRefracted = sampleScene(samplePx);
      } else {
        dryRefracted.r = sampleScene(samplePx + chromaShift).r;
        dryRefracted.g = sampleScene(samplePx).g;
        dryRefracted.b = sampleScene(samplePx - chromaShift).b;
      }

      /*
       * v7.8.2: the rain scene stays on the clear hydrangea foreground.  The
       * old low-frequency rain texture was intentionally pre-blurred and made
       * the whole viewport look like one sheet of fog.  Only the water map may
       * now bend the scene; untouched pixels keep the source image detail.
       */
      vec3 refracted = dryRefracted;
      if (rainBlend > 0.001) {
        // Always start from the untouched, full-resolution hydrangea source.
        vec3 rainScene = sampleRainForeground(samplePx);
        refracted = mix(dryRefracted, rainScene, rainBlend);

        vec4 water = sampleWater(screenPx, vec2(0.0));
        dropDepth = water.b;
        // KiraKiraAyu/RainEffect demo uses alphaMultiply=6, alphaSubtract=3.
        dropAlpha = clamp(water.a * 6.0 - 3.0, 0.0, 1.0);
        dropRefraction = (vec2(water.g, water.r) - vec2(0.5)) * 2.0;

        if (dropAlpha > 0.001) {
          // Upstream water.wgsl refraction: minRefraction 256, maxRefraction 512.
          float upstreamRefraction = 256.0 + dropDepth * 256.0;
          vec2 refractedPx = samplePx + dropRefraction * upstreamRefraction;
          vec3 throughDrop = sampleRainForeground(refractedPx);
          refracted = mix(refracted, throughDrop * 1.04, dropAlpha);

          // Upstream optional shadow is disabled in the demo.  Keep only its
          // optical refraction; no coloured fake reflection or purple smear.
          dropSpecular = 0.0;
        }
      }

      /* Jelly material: a transparent centre with a softly diffused body.
         DOM text remains above this canvas, so distortion never touches glyphs. */
      if (jellyMaterial > 0.5 && rainBlend < 0.55) {
        float jellySpread = 1.55 + rim * 2.15;
        vec3 jellyDiffuse = (
          sampleScene(samplePx + vec2(jellySpread, 0.0))
          + sampleScene(samplePx - vec2(jellySpread, 0.0))
          + sampleScene(samplePx + vec2(0.0, jellySpread))
          + sampleScene(samplePx - vec2(0.0, jellySpread))
        ) * 0.25;
        refracted = mix(refracted, jellyDiffuse, 0.12 + rim * 0.065);
        float jellyMilk = jellyMaterial * (0.004 + pow(rim, 1.15) * 0.022);
        refracted = mix(refracted, vec3(0.965, 0.975, 0.974), jellyMilk * 0.72);
      }

      float condensationAmount = 0.0;
      if (condensationAmount > 0.001) {
        /* Condensation changes local refraction instead of laying fog over the
           scene, so weather remains the same Crystal material. */
        vec3 microRefracted = sampleScene(
          samplePx + normal * (1.0 + condensationAmount * 2.0)
        );
        refracted = mix(refracted, microRefracted, condensationAmount * 0.035);
      }

      /* Calm content field: borrow the border-only idea used by mercury-rim UI.
         Strong bending / chroma remain on the rim, while the centre receives a
         small GPU-only low-frequency average.  This improves glyph contrast
         without restoring CSS/SVG glass or painting an opaque white card. */
      float contentField = (1.0 - smoothstep(0.16, 0.72, rim)) * u_targetEnabled;
      float calmSpread = mix(4.6, 5.4, iosMaterial) + jellyMaterial * 0.8;
      vec2 calmDiagonal = vec2(calmSpread * 0.7071);
      vec3 calmScene = sampleScene(samplePx) * 0.24;
      calmScene += (
        sampleScene(samplePx + vec2(calmSpread, 0.0))
        + sampleScene(samplePx - vec2(calmSpread, 0.0))
        + sampleScene(samplePx + vec2(0.0, calmSpread))
        + sampleScene(samplePx - vec2(0.0, calmSpread))
      ) * 0.12;
      calmScene += (
        sampleScene(samplePx + calmDiagonal)
        + sampleScene(samplePx - calmDiagonal)
        + sampleScene(samplePx + vec2(-calmDiagonal.x, calmDiagonal.y))
        + sampleScene(samplePx + vec2(calmDiagonal.x, -calmDiagonal.y))
      ) * 0.07;
      float calmLum = dot(calmScene, vec3(0.2126, 0.7152, 0.0722));
      calmScene = mix(vec3(calmLum), calmScene, 0.72);
      calmScene = (calmScene - 0.5) * 0.91 + 0.5;
      calmScene = mix(calmScene, vec3(0.972, 0.982, 0.978), 0.09);
      float calmMix = contentField * mix(0.36, 0.31, jellyMaterial);
      refracted = mix(refracted, calmScene, calmMix);
      float readabilityLift = contentField * clamp(
        0.055 + (0.60 - calmLum) * 0.11,
        0.045,
        0.105
      );
      refracted = mix(refracted, vec3(0.975, 0.987, 0.984), readabilityLift);

      /* 不再在整面湿窗铺高亮白雾，也不做全屏伪锐化。源图的柔和
         细节原样保留；高光只出现在真正的水滴和玻璃边缘。 */
      float dryPolish = 1.0 - rainBlend;
      float luminance = dot(
        refracted,
        vec3(0.2126, 0.7152, 0.0722)
      );
      refracted = mix(
        vec3(luminance),
        refracted,
        1.0 + 0.025 * dryPolish
      );
      refracted *= 1.0 + 0.008 * dryPolish;

      /* 只有水晶厚边带一点薄荷白，不在中央铺白雾。 */
      refracted = mix(
        refracted,
        vec3(0.88, 1.0, 0.99),
        rim * 0.018 + crystalEdge * 0.021
      );

      /* Clear object boundary without CSS borders: a 5px GPU silver crest plus
         a narrow dark inner crease.  This survives both light and dark scenes. */
      float outerContour = 1.0 - smoothstep(0.30, 4.4, depth);
      float innerCrease = smoothstep(3.2, 5.1, depth)
        * (1.0 - smoothstep(5.1, 9.4, depth));
      float contourLight = pow(max(dot(-normal, sharedLightDir), 0.0), 3.8);
      float contourShade = pow(max(dot(normal, sharedLightDir), 0.0), 2.6);
      refracted += vec3(0.34, 0.42, 0.43)
        * outerContour * (0.21 + contourLight * 0.40) * mobileMaterialFactor;
      refracted -= vec3(innerCrease * (0.032 + contourShade * 0.044));

      /* The environment reflects back from the curved edge. The centre remains
         transparent; Fresnel response grows only toward the rim. */
      float fresnel = pow(clamp(rim, 0.0, 1.0), mix(1.58, 1.34, jellyMaterial))
        * fresnelStrength
        * mobileMaterialFactor
        * mix(1.0, 1.28, jellyMaterial);
      vec3 reflectedEnvironment = sampleScene(
        screenPx - sharedLightDir * (4.0 + rim * 11.0 * refractionScale)
      );
      refracted = mix(
        refracted,
        reflectedEnvironment * 1.012,
        fresnel * reflectionStrength
      );

      /* iOS-style small glass: clear centre, thick optically active rim. */
      float iosInnerRim = iosMaterial * pow(rim, 1.28);
      float iosTopLight = iosMaterial * pow(
        max(dot(-normal, sharedLightDir), 0.0),
        5.5
      ) * iosInnerRim;
      float iosLowerShade = iosMaterial * pow(
        max(dot(-normal, -sharedLightDir), 0.0),
        4.2
      ) * iosInnerRim;
      refracted = mix(
        refracted,
        vec3(0.94, 0.985, 1.0),
        iosMaterial * (0.003 + iosInnerRim * 0.026)
      );
      refracted += iosTopLight * vec3(0.205, 0.218, 0.22);
      refracted -= iosLowerShade * vec3(0.025, 0.034, 0.038);

      float jellyBulge = jellyMaterial * pow(clamp(rim, 0.0, 1.0), 1.12);
      float jellyTop = pow(max(dot(-normal, sharedLightDir), 0.0), 4.4) * jellyBulge;
      float jellyBottom = pow(max(dot(-normal, -sharedLightDir), 0.0), 3.8) * jellyBulge;
      refracted += jellyTop * vec3(0.090, 0.105, 0.102);
      refracted -= jellyBottom * vec3(0.015, 0.024, 0.018);

      float directional = pow(
        max(dot(-normal, sharedLightDir), 0.0),
        7.0
      );
      float corner = smoothstep(
        0.76,
        1.16,
        length(relative)
      );
      float broadReflection = pow(rim, 1.45)
        * (0.018 + directional * 0.085)
        * (0.72 + reflectionStrength);
      float specular =
        broadReflection
        + corner * rim * 0.033
        + pointerLift * crystalEdge * 0.062 * mobileMaterialFactor
        + iosMaterial * iosInnerRim * (0.021 + directional * 0.095) * mobileMaterialFactor;
      refracted += specular * vec3(0.38, 0.46, 0.45);
      refracted += dropSpecular * vec3(0.965, 0.972, 0.970);

      refracted += skyFlash(u_weatherOptics.w)
        * u_weatherOptics.z
        * vec3(0.2, 0.25, 0.31);

      gl_FragColor = vec4(clamp(refracted, 0.0, 1.0), 1.0);
    }
  `;

  const PRISM_VERTEX_SOURCE = `
    attribute vec2 a_prismPosition;
    attribute vec3 a_prismNormal;
    varying vec3 v_prismNormal;

    void main() {
      v_prismNormal = a_prismNormal;
      gl_Position = vec4(a_prismPosition, 0.0, 1.0);
    }
  `;

  const PRISM_FRAGMENT_SOURCE = `
    precision mediump float;
    varying vec3 v_prismNormal;
    uniform vec2 u_prismLight;
    uniform vec3 u_prismTint;
    uniform float u_prismAlpha;

    void main() {
      vec3 normal = normalize(v_prismNormal);
      vec3 lightDir = normalize(vec3(u_prismLight, 0.86));
      float side = 1.0 - abs(normal.z);
      float facing = max(dot(normal, lightDir), 0.0);
      float grazing = pow(clamp(side, 0.0, 1.0), 1.45);
      vec3 neutral = vec3(0.90, 0.965, 0.958);
      vec3 colour = mix(neutral, u_prismTint, 0.055);
      colour += facing * vec3(0.070, 0.082, 0.080);
      colour -= max(dot(normal, -lightDir), 0.0) * vec3(0.018, 0.024, 0.022);
      float alpha = u_prismAlpha * (0.30 + grazing * 0.72) + facing * 0.018;
      gl_FragColor = vec4(clamp(colour, 0.0, 1.0), clamp(alpha, 0.0, 0.19));
    }
  `;

  let canvas;
  let gl;
  let program;
  let buffer;
  let prismProgram;
  let prismBuffer;
  let prismLocations;
  let texture;
  let rainForegroundTexture;
  let rainBackgroundTexture;
  let waterTexture;
  let waterTextureWidth = 0;
  let waterTextureHeight = 0;
  let lastUploadedWaterFrame = -1;
  let image;
  let rainForegroundImage;
  let rainBackgroundImage;
  let imageSetKey = '';
  let loadingUrl = '';
  let imagePromise = null;
  let rainMap = null;
  let rainMapPromise = null;
  let raf = 0;
  let weatherRaf = 0;
  let weatherDrawRequested = false;
  let weatherLastDrawAt = 0;
  let drawing = false;
  let drawQueued = false;
  let disabled = false;
  let contextLost = false;
  let pointerActive = false;
  let pointerX = -10000;
  let pointerY = -10000;
  let pointerTarget = null;
  let effectiveDpr = 1;
  let fullRedrawRequested = true;
  let pointerDirtyNodes = new Set();
  let targetNodes = [];
  let resizeObserver = null;
  let mutationObserver = null;
  let domMutationObserver = null;
  let uiMotionUntil = 0;
  let uniforms = null;

  const reduceMotionQuery = window.matchMedia(
    '(prefers-reduced-motion: reduce)'
  );

  function isMobile() {
    return window.innerWidth <= MOBILE_BREAKPOINT;
  }

  function isRenderActive() {
    return Boolean(document.body)
      && document.documentElement.dataset.uiMode !== 'reader';
  }

  function isTargetEligible(node) {
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

  function compileShader(type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      const message = gl.getShaderInfoLog(shader)
        || 'unknown shader error';
      gl.deleteShader(shader);
      throw new Error(message);
    }
    return shader;
  }

  function createProgram() {
    const vertex = compileShader(
      gl.VERTEX_SHADER,
      VERTEX_SOURCE
    );
    const fragment = compileShader(
      gl.FRAGMENT_SHADER,
      FRAGMENT_SOURCE
    );
    const result = gl.createProgram();
    gl.attachShader(result, vertex);
    gl.attachShader(result, fragment);
    gl.linkProgram(result);
    gl.deleteShader(vertex);
    gl.deleteShader(fragment);
    if (!gl.getProgramParameter(result, gl.LINK_STATUS)) {
      const message = gl.getProgramInfoLog(result)
        || 'unknown link error';
      gl.deleteProgram(result);
      throw new Error(message);
    }
    return result;
  }

  function createPrismProgram() {
    const vertex = compileShader(gl.VERTEX_SHADER, PRISM_VERTEX_SOURCE);
    const fragment = compileShader(gl.FRAGMENT_SHADER, PRISM_FRAGMENT_SOURCE);
    const result = gl.createProgram();
    gl.attachShader(result, vertex);
    gl.attachShader(result, fragment);
    gl.linkProgram(result);
    gl.deleteShader(vertex);
    gl.deleteShader(fragment);
    if (!gl.getProgramParameter(result, gl.LINK_STATUS)) {
      const message = gl.getProgramInfoLog(result) || 'unknown prism link error';
      gl.deleteProgram(result);
      throw new Error(message);
    }
    return result;
  }

  function prismLocationMap() {
    return {
      position: gl.getAttribLocation(prismProgram, 'a_prismPosition'),
      normal: gl.getAttribLocation(prismProgram, 'a_prismNormal'),
      light: gl.getUniformLocation(prismProgram, 'u_prismLight'),
      tint: gl.getUniformLocation(prismProgram, 'u_prismTint'),
      alpha: gl.getUniformLocation(prismProgram, 'u_prismAlpha'),
    };
  }

  function locations() {
    return {
      position: gl.getAttribLocation(program, 'a_position'),
      scene: gl.getUniformLocation(program, 'u_scene'),
      sceneRainFg: gl.getUniformLocation(program, 'u_sceneRainFg'),
      sceneRainBg: gl.getUniformLocation(program, 'u_sceneRainBg'),
      waterMap: gl.getUniformLocation(program, 'u_waterMap'),
      viewport: gl.getUniformLocation(program, 'u_viewport'),
      imageSize: gl.getUniformLocation(program, 'u_imageSize'),
      bgPosition: gl.getUniformLocation(program, 'u_bgPosition'),
      rainImageSize: gl.getUniformLocation(program, 'u_rainImageSize'),
      rainBgPosition: gl.getUniformLocation(program, 'u_rainBgPosition'),
      sceneBlurPx: gl.getUniformLocation(program, 'u_sceneBlurPx'),
      weatherSurface: gl.getUniformLocation(program, 'u_weatherSurface'),
      weatherColor: gl.getUniformLocation(program, 'u_weatherColor'),
      weatherOptics: gl.getUniformLocation(program, 'u_weatherOptics'),
      weatherAtmosphere: gl.getUniformLocation(program, 'u_weatherAtmosphere'),
      targetRect: gl.getUniformLocation(program, 'u_targetRect'),
      targetRadius: gl.getUniformLocation(program, 'u_targetRadius'),
      targetEnabled: gl.getUniformLocation(program, 'u_targetEnabled'),
      targetMaterial: gl.getUniformLocation(program, 'u_targetMaterial'),
      lightDirection: gl.getUniformLocation(program, 'u_lightDirection'),
      materialParams: gl.getUniformLocation(program, 'u_materialParams'),
      pointer: gl.getUniformLocation(program, 'u_pointer'),
    };
  }

  function createTexture(source) {
    const nextTexture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, nextTexture);
    gl.texParameteri(
      gl.TEXTURE_2D,
      gl.TEXTURE_WRAP_S,
      gl.CLAMP_TO_EDGE
    );
    gl.texParameteri(
      gl.TEXTURE_2D,
      gl.TEXTURE_WRAP_T,
      gl.CLAMP_TO_EDGE
    );
    gl.texParameteri(
      gl.TEXTURE_2D,
      gl.TEXTURE_MIN_FILTER,
      gl.LINEAR
    );
    gl.texParameteri(
      gl.TEXTURE_2D,
      gl.TEXTURE_MAG_FILTER,
      gl.LINEAR
    );
    gl.pixelStorei(
      gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL,
      false
    );
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    // iOS Safari has shipped GPU/decoder combinations where uploading a JPEG
    // straight into RGB drops/corrupts its blue channel.  Expanding the scene
    // texture to RGBA avoids that driver path without touching the source art.
    gl.getError();
    try {
      gl.texImage2D(
        gl.TEXTURE_2D,
        0,
        gl.RGBA,
        gl.RGBA,
        gl.UNSIGNED_BYTE,
        source
      );
    } catch (error) {
      gl.deleteTexture(nextTexture);
      throw error;
    }
    const uploadError = gl.getError();
    if (uploadError !== gl.NO_ERROR) {
      gl.deleteTexture(nextTexture);
      throw new Error(`WebGL scene texture upload failed: ${uploadError}`);
    }
    return nextTexture;
  }

  function createWaterTexture() {
    const nextTexture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, nextTexture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    return nextTexture;
  }

  function currentImageSet() {
    return {
      scene: '/static/images/sunny-street-desktop.jpg',
      foreground: '/static/images/crystal-street-desktop.jpg',
      background: '/static/images/crystal-street-desktop.jpg',
    };
  }

  function loadImage(url) {
    return new Promise((resolve, reject) => {
      const next = new Image();
      next.decoding = 'async';
      next.onload = () => resolve(next);
      next.onerror = () => reject(
        new Error(`无法载入 WebGL 背景：${url}`)
      );
      next.src = url;
    });
  }

  async function ensureImage() {
    const wanted = currentImageSet();
    const wantedKey = [wanted.scene, wanted.foreground, wanted.background].join('|');
    if (
      image
      && wantedKey === imageSetKey
      && texture
      && rainForegroundTexture
      && rainBackgroundTexture
    ) return;

    if (!imagePromise || loadingUrl !== wantedKey) {
      loadingUrl = wantedKey;
      imagePromise = Promise.all([
        loadImage(wanted.scene),
        loadImage(wanted.foreground),
        loadImage(wanted.background),
      ]);
    }

    const next = await imagePromise;
    const current = currentImageSet();
    const currentKey = [current.scene, current.foreground, current.background].join('|');
    if (wantedKey !== currentKey) {
      imagePromise = null;
      return ensureImage();
    }

    [image, rainForegroundImage, rainBackgroundImage] = next;
    imageSetKey = wantedKey;
    imagePromise = null;
    loadingUrl = '';
    if (texture) gl.deleteTexture(texture);
    if (rainForegroundTexture) gl.deleteTexture(rainForegroundTexture);
    if (rainBackgroundTexture) gl.deleteTexture(rainBackgroundTexture);
    texture = createTexture(image);
    rainForegroundTexture = createTexture(rainForegroundImage);
    rainBackgroundTexture = createTexture(rainBackgroundImage);
  }

  function rainMapScale() {
    // Water maps carry normals/depth, not final colour.  Keep the original
    // 1080p density, but cap invisible over-resolution on 2K/4K desktops.
    const preferred = isMobile() ? .68 : .78;
    const viewportPixels = Math.max(1, window.innerWidth * window.innerHeight);
    const maxWaterPixels = isMobile() ? 820_000 : 1_220_000;
    const pixelCapped = Math.sqrt(maxWaterPixels / viewportPixels);
    const minimum = isMobile() ? .50 : .36;
    return Math.max(minimum, Math.min(preferred, pixelCapped));
  }

  async function ensureRainMap() {
    const scale = rainMapScale();
    const width = Math.max(1, Math.round(window.innerWidth * scale));
    const height = Math.max(1, Math.round(window.innerHeight * scale));
    if (!rainMapPromise) {
      if (!window.DaxiguaRainMap?.create) {
        throw new Error('RainEffect water-map generator unavailable');
      }
      rainMapPromise = window.DaxiguaRainMap.create({
        width,
        height,
        scale,
      });
    }
    rainMap = await rainMapPromise;
    rainMap.resize(width, height, scale);
  }

  function uploadWaterMap(weather) {
    rainMap.setWeather(reduceMotionQuery.matches
      ? { ...weather, precipitation: 0 }
      : weather);
    gl.activeTexture(gl.TEXTURE3);
    if (!waterTexture) waterTexture = createWaterTexture();
    gl.bindTexture(gl.TEXTURE_2D, waterTexture);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);

    if (
      waterTextureWidth !== rainMap.canvas.width
      || waterTextureHeight !== rainMap.canvas.height
    ) {
      waterTextureWidth = rainMap.canvas.width;
      waterTextureHeight = rainMap.canvas.height;
      gl.texImage2D(
        gl.TEXTURE_2D,
        0,
        gl.RGBA,
        gl.RGBA,
        gl.UNSIGNED_BYTE,
        rainMap.canvas
      );
      lastUploadedWaterFrame = rainMap.frameVersion;
      return;
    }

    // The WebGL scene can render more often than the water simulation.
    // Do not resend an identical multi-megabyte canvas to the GPU.
    if (lastUploadedWaterFrame === rainMap.frameVersion) return;
    gl.texSubImage2D(
      gl.TEXTURE_2D,
      0,
      0,
      0,
      gl.RGBA,
      gl.UNSIGNED_BYTE,
      rainMap.canvas
    );
    lastUploadedWaterFrame = rainMap.frameVersion;
  }

  function refreshTargets() {
    document.querySelectorAll('.liquid-webgl-target')
      .forEach((node) => node.classList.remove('liquid-webgl-target'));
    resizeObserver?.disconnect();
    // 晴雨景观界面继续使用真实折射、Fresnel 与环境反射。
    // 阅读界面只暂停绘制，不删除天气状态；退出时会按原天气恢复。
    const selectors = DESKTOP_TARGETS;
    const candidates = selectors
      .flatMap((selector) => Array.from(document.querySelectorAll(selector)))
      .filter((node, index, list) => list.indexOf(node) === index)
      .filter(isTargetEligible);
    // One GPU material per visible surface. If a target is already inside
    // another GPU surface, the child keeps ordinary CSS chrome instead of
    // being refracted a second time. This avoids the old milky parent/child
    // double-composition while keeping standalone bubbles and controls GPU-backed.
    const candidateSet = new Set(candidates);
    targetNodes = candidates.filter((node) => {
      let parent = node.parentElement;
      while (parent) {
        if (candidateSet.has(parent)) return false;
        parent = parent.parentElement;
      }
      return true;
    });
    targetNodes.forEach((node) => {
      node.classList.add('liquid-webgl-target');
      resizeObserver?.observe(node);
    });
  }

  function collectTargetRects() {
    return targetNodes.map((node) => {
      if (!isTargetEligible(node)) return null;
      const rect = node.getBoundingClientRect();
      if (
        rect.width < 2
        || rect.height < 2
        || rect.right <= 0
        || rect.bottom <= 0
        || rect.left >= window.innerWidth
        || rect.top >= window.innerHeight
      ) return null;
      const style = window.getComputedStyle(node);
      const radius = Math.max(0, parseFloat(style.borderTopLeftRadius) || 0);
      const clipLeft = Math.max(0, rect.left);
      const clipTop = Math.max(0, rect.top);
      const clipRight = Math.min(window.innerWidth, rect.right);
      const clipBottom = Math.min(window.innerHeight, rect.bottom);
      return {
        node,
        // Keep the optical geometry in the card's original coordinate space.
        // Clipping a partly off-screen card must never move its centre/radius.
        x: rect.left,
        y: rect.top,
        width: rect.width,
        height: rect.height,
        clipX: clipLeft,
        clipY: clipTop,
        clipWidth: clipRight - clipLeft,
        clipHeight: clipBottom - clipTop,
        radius,
        material: node.matches('.jty-jelly-target')
          ? 2
          : node.matches('.jty-space-portal, .intimacy-vitals')
            ? 1 : 0,
      };
    }).filter((rect) => rect && rect.clipWidth > 1 && rect.clipHeight > 1);
  }

  function setTargetUniforms(rect, active) {
    if (!active || !rect) {
      gl.uniform4f(uniforms.targetRect, 0, 0, window.innerWidth, window.innerHeight);
      gl.uniform1f(uniforms.targetRadius, 0);
      gl.uniform1f(uniforms.targetEnabled, 0);
      gl.uniform1f(uniforms.targetMaterial, 0);
      gl.uniform3f(uniforms.pointer, -10000, -10000, 0);
      return;
    }
    gl.uniform4f(uniforms.targetRect, rect.x, rect.y, rect.width, rect.height);
    gl.uniform1f(uniforms.targetRadius, Math.min(rect.radius, rect.width / 2, rect.height / 2));
    gl.uniform1f(uniforms.targetEnabled, 1);
    gl.uniform1f(uniforms.targetMaterial, rect.material || 0);
    const pointerOnTarget = pointerActive && pointerTarget === rect.node;
    gl.uniform3f(
      uniforms.pointer, pointerX, pointerY, pointerOnTarget ? 1 : 0
    );
  }

  function scissorRect(rect) {
    const x = Math.max(0, Math.floor(rect.clipX * effectiveDpr));
    const y = Math.max(0, Math.floor(
      (window.innerHeight - rect.clipY - rect.clipHeight) * effectiveDpr
    ));
    const width = Math.max(1, Math.ceil(rect.clipWidth * effectiveDpr));
    const height = Math.max(1, Math.ceil(rect.clipHeight * effectiveDpr));
    gl.scissor(x, y, width, height);
  }


  function calculateDpr() {
    const nativeDpr = Math.min(
      window.devicePixelRatio || 1,
      DPR_LIMIT
    );
    const area = Math.max(
      1,
      window.innerWidth * window.innerHeight
    );
    const pixelCappedDpr = Math.sqrt(
      MAX_BACKBUFFER_PIXELS / area
    );
    return Math.max(
      0.85,
      Math.min(nativeDpr, pixelCappedDpr)
    );
  }

  function numberData(node, name, fallback) {
    const value = Number.parseFloat(node?.dataset?.[name] || '');
    return Number.isFinite(value) ? value : fallback;
  }

  function roundedPerimeter(width, height, radius, segments = 5) {
    const halfW = Math.max(1, width * 0.5);
    const halfH = Math.max(1, height * 0.5);
    const r = Math.max(0, Math.min(radius, halfW, halfH));
    const points = [];
    const corners = [
      [halfW - r, -halfH + r, -Math.PI * .5, 0],
      [halfW - r, halfH - r, 0, Math.PI * .5],
      [-halfW + r, halfH - r, Math.PI * .5, Math.PI],
      [-halfW + r, -halfH + r, Math.PI, Math.PI * 1.5],
    ];
    corners.forEach(([cx, cy, start, end]) => {
      for (let i = 0; i <= segments; i += 1) {
        const t = start + (end - start) * (i / segments);
        points.push([cx + Math.cos(t) * r, cy + Math.sin(t) * r]);
      }
    });
    return points;
  }

  function rotate3(point, rx, ry, rz) {
    let [x, y, z] = point;
    const sx = Math.sin(rx); const cx = Math.cos(rx);
    const sy = Math.sin(ry); const cy = Math.cos(ry);
    const sz = Math.sin(rz); const cz = Math.cos(rz);
    let ny = y * cx - z * sx;
    let nz = y * sx + z * cx;
    y = ny; z = nz;
    let nx = x * cy + z * sy;
    nz = -x * sy + z * cy;
    x = nx; z = nz;
    nx = x * cz - y * sz;
    ny = x * sz + y * cz;
    return [nx, ny, z];
  }

  function projectedPrismVertex(x, y, z, normal, params) {
    const p = rotate3([x, y, z], params.rx, params.ry, params.rz);
    const n = rotate3(normal, params.rx, params.ry, params.rz);
    const focal = 920;
    const perspective = focal / Math.max(520, focal - p[2]);
    const screenX = params.cx + p[0] * perspective;
    const screenY = params.cy + p[1] * perspective;
    return [
      screenX / Math.max(1, window.innerWidth) * 2 - 1,
      1 - screenY / Math.max(1, window.innerHeight) * 2,
      n[0], n[1], n[2],
    ];
  }

  function prismMesh(node) {
    const rect = node.getBoundingClientRect();
    if (
      rect.width < 8 || rect.height < 8 || rect.right <= 0 || rect.bottom <= 0
      || rect.left >= window.innerWidth || rect.top >= window.innerHeight
    ) return null;
    const style = window.getComputedStyle(node);
    const radius = Math.max(2, Number.parseFloat(style.borderTopLeftRadius) || 12);
    const thickness = Math.max(3, Math.min(18, numberData(node, 'glassThickness', 8)));
    const params = {
      cx: rect.left + rect.width * .5,
      cy: rect.top + rect.height * .5,
      rx: numberData(node, 'glassRx', .7) * Math.PI / 180,
      ry: numberData(node, 'glassRy', -.65) * Math.PI / 180,
      rz: numberData(node, 'glassRz', 0) * Math.PI / 180,
    };
    const points = roundedPerimeter(rect.width, rect.height, radius, rect.width > 500 ? 6 : 5);
    const vertices = [];
    const bevel = Math.max(1.5, Math.min(4, thickness * .26));
    const innerScaleX = Math.max(.75, (rect.width - bevel * 2) / rect.width);
    const innerScaleY = Math.max(.75, (rect.height - bevel * 2) / rect.height);
    const push = (point, normal) => vertices.push(...projectedPrismVertex(point[0], point[1], point[2], normal, params));

    for (let i = 0; i < points.length; i += 1) {
      const p0 = points[i];
      const p1 = points[(i + 1) % points.length];
      const mx = (p0[0] + p1[0]) * .5 / Math.max(1, rect.width * .5);
      const my = (p0[1] + p1[1]) * .5 / Math.max(1, rect.height * .5);
      const length = Math.hypot(mx, my) || 1;
      const sideNormal = [mx / length, my / length, .05];
      const f0 = [p0[0], p0[1], 0];
      const f1 = [p1[0], p1[1], 0];
      const b0 = [p0[0], p0[1], -thickness];
      const b1 = [p1[0], p1[1], -thickness];
      push(f0, sideNormal); push(b0, sideNormal); push(f1, sideNormal);
      push(f1, sideNormal); push(b0, sideNormal); push(b1, sideNormal);

      const i0 = [p0[0] * innerScaleX, p0[1] * innerScaleY, -bevel];
      const i1 = [p1[0] * innerScaleX, p1[1] * innerScaleY, -bevel];
      const bevelNormal = [sideNormal[0] * .34, sideNormal[1] * .34, .94];
      push(f0, bevelNormal); push(f1, bevelNormal); push(i0, bevelNormal);
      push(f1, bevelNormal); push(i1, bevelNormal); push(i0, bevelNormal);
    }
    return new Float32Array(vertices);
  }

  function drawPrismMeshes() {
    if (!prismProgram || !prismBuffer || !prismLocations) return;
    const targetNodeSet = new Set(targetNodes);
    const nodes = Array.from(document.querySelectorAll('.jty-3d-glass-target'))
      // A 3D bevel belongs to the same optical owner as the refracted face.
      // Never draw a nested child after its parent has already been rendered.
      .filter((node) => targetNodeSet.has(node))
      .filter((node) => {
        const style = window.getComputedStyle(node);
        return style.display !== 'none' && style.visibility !== 'hidden' && Number.parseFloat(style.opacity || '1') > .01;
      })
      .slice(0, 8);
    if (!nodes.length) return;

    gl.disable(gl.SCISSOR_TEST);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    gl.useProgram(prismProgram);
    gl.bindBuffer(gl.ARRAY_BUFFER, prismBuffer);
    gl.enableVertexAttribArray(prismLocations.position);
    gl.enableVertexAttribArray(prismLocations.normal);
    gl.vertexAttribPointer(prismLocations.position, 2, gl.FLOAT, false, 20, 0);
    gl.vertexAttribPointer(prismLocations.normal, 3, gl.FLOAT, false, 20, 8);
    gl.uniform2f(prismLocations.light, -.38, -.84);
    gl.uniform3f(prismLocations.tint, .79, .91, .90);

    nodes.forEach((node) => {
      const mesh = prismMesh(node);
      if (!mesh || mesh.length < 15) return;
      gl.bufferData(gl.ARRAY_BUFFER, mesh, gl.DYNAMIC_DRAW);
      const jelly = node.classList.contains('jty-jelly-target') || node.classList.contains('jty-jelly-control');
      const plane = node.classList.contains('jty-life-plane');
      gl.uniform1f(prismLocations.alpha, plane ? .046 : jelly ? .128 : .072);
      gl.drawArrays(gl.TRIANGLES, 0, mesh.length / 5);
    });

    gl.disable(gl.BLEND);
  }

  function resizeCanvas() {
    effectiveDpr = calculateDpr();
    const width = Math.max(
      1,
      Math.round(window.innerWidth * effectiveDpr)
    );
    const height = Math.max(
      1,
      Math.round(window.innerHeight * effectiveDpr)
    );
    let resized = false;
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
      resized = true;
    }
    canvas.style.width = `${window.innerWidth}px`;
    canvas.style.height = `${window.innerHeight}px`;
    gl.viewport(0, 0, width, height);
    return resized;
  }

  function weatherParams() {
    return window.DaxiguaWeather?.params || {
      exposure: 1.04,
      temperature: .12,
      contrast: 1.02,
      saturation: 1.04,
      fog: .02,
      lowFog: .01,
      cloudCover: .04,
      cloudDepth: .06,
      glassWetness: 0,
      glassFlow: 0,
      condensation: 0,
      reflection: .18,
      precipitation: 0,
      lightning: 0,
    };
  }

  function sceneBlurPixels() {
    const weatherValue = Number(window.DaxiguaWeather?.blurPixels);
    if (Number.isFinite(weatherValue)) return Math.max(0, weatherValue);
    const cssValue = Number.parseFloat(
      getComputedStyle(document.documentElement)
        .getPropertyValue('--weather-scene-blur-px')
    );
    return Number.isFinite(cssValue) ? Math.max(0, cssValue) : 0;
  }

  function materialParams() {
    const mobile = isMobile();
    return {
      lightX: -0.68,
      lightY: -0.73,
      refraction: mobile ? .72 : 1,
      fresnel: mobile ? .16 : .21,
      reflection: mobile ? .21 : .28,
      mobileFactor: mobile ? .82 : 1,
    };
  }

  function weatherAnimationActive() {
    if (
      disabled
      || contextLost
      || document.hidden
      || !isRenderActive()
      || reduceMotionQuery.matches
    ) return false;
    const weather = weatherParams();
    return window.DaxiguaWeather?.phase === 'transitioning' || (
      weather.glassWetness > .04
      && weather.precipitation > .02
    ) || weather.lightning > .02;
  }

  function preferredWeatherFps() {
    // A full-screen scene plus every visible mother-card is expensive even on
    // Apple Silicon. Weather motion does not need 45–60 redraws per second.
    if (isMobile()) return 24;
    const area = window.innerWidth * window.innerHeight;
    if (area >= 3_000_000) return 24;
    return 30;
  }

  function scheduleWeatherAnimation() {
    if (weatherRaf || !weatherAnimationActive()) return;
    weatherRaf = window.requestAnimationFrame(weatherAnimationFrame);
  }

  function weatherAnimationFrame(now) {
    weatherRaf = 0;
    if (!weatherAnimationActive()) return;
    const targetFps = preferredWeatherFps();
    if (!weatherLastDrawAt || now - weatherLastDrawAt >= 1000 / targetFps) {
      weatherLastDrawAt = now;
      requestWeatherDraw();
    }
    scheduleWeatherAnimation();
  }

  async function draw() {
    raf = 0;
    if (
      disabled
      || contextLost
      || !gl
      || document.hidden
      || !isRenderActive()
    ) {
      return;
    }
    if (drawing) {
      drawQueued = true;
      return;
    }
    drawing = true;

    try {
      fullRedrawRequested = false;
      weatherDrawRequested = false;
      pointerDirtyNodes.clear();

      await ensureImage();
      if (
        disabled
        || contextLost
        || !isRenderActive()
      ) {
        return;
      }

      resizeCanvas();
      await ensureRainMap();
      const weather = weatherParams();
      const material = materialParams();
      const time = reduceMotionQuery.matches ? 0 : performance.now() / 1000;
      uploadWaterMap(weather);

      gl.useProgram(program);
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.enableVertexAttribArray(uniforms.position);
      gl.vertexAttribPointer(
        uniforms.position,
        2,
        gl.FLOAT,
        false,
        0,
        0
      );

      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.uniform1i(uniforms.scene, 0);
      gl.activeTexture(gl.TEXTURE1);
      gl.bindTexture(gl.TEXTURE_2D, rainForegroundTexture);
      gl.uniform1i(uniforms.sceneRainFg, 1);
      gl.activeTexture(gl.TEXTURE2);
      gl.bindTexture(gl.TEXTURE_2D, rainBackgroundTexture);
      gl.uniform1i(uniforms.sceneRainBg, 2);
      gl.activeTexture(gl.TEXTURE3);
      gl.bindTexture(gl.TEXTURE_2D, waterTexture);
      gl.uniform1i(uniforms.waterMap, 3);
      gl.uniform2f(
        uniforms.viewport,
        window.innerWidth,
        window.innerHeight
      );
      gl.uniform2f(
        uniforms.imageSize,
        image.naturalWidth || image.width,
        image.naturalHeight || image.height
      );
      gl.uniform2f(
        uniforms.bgPosition,
        0.5,
        0.5
      );
      gl.uniform2f(
        uniforms.rainImageSize,
        rainForegroundImage.naturalWidth || rainForegroundImage.width,
        rainForegroundImage.naturalHeight || rainForegroundImage.height
      );
      gl.uniform2f(
        uniforms.rainBgPosition,
        isMobile() ? 0.61 : 0.5,
        0.5
      );
      gl.uniform1f(uniforms.sceneBlurPx, sceneBlurPixels());
      gl.uniform4f(
        uniforms.weatherSurface,
        weather.glassWetness,
        weather.glassFlow,
        weather.precipitation,
        weather.reflection
      );
      gl.uniform4f(
        uniforms.weatherColor,
        weather.exposure,
        weather.saturation,
        weather.temperature,
        weather.fog
      );
      gl.uniform4f(
        uniforms.weatherOptics,
        weather.condensation,
        weather.contrast,
        weather.lightning,
        time
      );
      gl.uniform4f(
        uniforms.weatherAtmosphere,
        weather.cloudCover,
        weather.cloudDepth,
        weather.lowFog,
        weather.reflection
      );
      gl.uniform2f(
        uniforms.lightDirection,
        material.lightX,
        material.lightY
      );
      gl.uniform4f(
        uniforms.materialParams,
        material.refraction,
        material.fresnel,
        material.reflection,
        material.mobileFactor
      );

      gl.disable(gl.DEPTH_TEST);
      gl.disable(gl.BLEND);
      // A full clear follows immediately, so clearing old target rectangles
      // separately would only duplicate GPU work.
      gl.disable(gl.SCISSOR_TEST);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      setTargetUniforms(null, false);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);

      const currentRects = collectTargetRects();
      gl.enable(gl.SCISSOR_TEST);
      currentRects.forEach((rect) => {
        scissorRect(rect);
        setTargetUniforms(rect, true);
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      });
      gl.disable(gl.SCISSOR_TEST);
      drawPrismMeshes();
      document.documentElement.classList.add(
        'webgl-glass-ready',
        'gpu-glass-ready'
      );
      document.documentElement.classList.remove('webgpu-glass-ready');
      document.documentElement.dataset.glassRenderer = 'webgl';
      document.documentElement.dataset.weatherGlass = 'webgl';
      document.documentElement.dataset.weatherRainEngine = 'raineffect-watermap';
    } catch (error) {
      console.warn('[CrystalClear] WebGL draw failed', error);
      fallback();
    } finally {
      drawing = false;
      if (drawQueued) {
        drawQueued = false;
        requestDraw();
      }
      if (performance.now() < uiMotionUntil) requestDraw();
      scheduleWeatherAnimation();
    }
  }

  function requestDraw() {
    fullRedrawRequested = true;
    if (
      disabled
      || contextLost
      || raf
      || !isRenderActive()
    ) {
      return;
    }
    raf = window.requestAnimationFrame(draw);
  }

  function requestPointerDraw(...nodes) {
    for (const node of nodes) {
      if (node) pointerDirtyNodes.add(node);
    }
    if (
      disabled
      || contextLost
      || raf
      || !isRenderActive()
    ) {
      return;
    }
    raf = window.requestAnimationFrame(draw);
  }

  function requestWeatherDraw() {
    weatherDrawRequested = true;
    if (
      disabled
      || contextLost
      || raf
      || !isRenderActive()
    ) return;
    raf = window.requestAnimationFrame(draw);
  }

  function setPointerInactive() {
    if (!pointerActive) return;
    const previousTarget = pointerTarget;
    pointerActive = false;
    pointerTarget = null;
    requestPointerDraw(previousTarget);
  }

  function fallback() {
    disabled = true;
    pointerActive = false;
    if (weatherRaf) cancelAnimationFrame(weatherRaf);
    weatherRaf = 0;
    weatherLastDrawAt = 0;
    document.documentElement.classList.remove(
      'webgl-glass-ready',
      'gpu-glass-ready'
    );
    delete document.documentElement.dataset.glassRenderer;
    document.documentElement.dataset.weatherGlass = 'none';
    document.documentElement.dataset.weatherRainEngine = 'none';
    rainMap?.destroy?.();
    domMutationObserver?.disconnect();
    if (canvas) canvas.style.display = 'none';
  }

  function bindEvents() {
    const onResize = () => {
      refreshTargets();
      requestDraw();
    };
    window.addEventListener('resize', onResize, { passive: true });
    window.addEventListener(
      'orientationchange',
      onResize,
      { passive: true }
    );
    window.visualViewport?.addEventListener(
      'resize',
      onResize,
      { passive: true }
    );

    let lastScrollSample = 0;
    document.addEventListener('scroll', () => {
      const now = performance.now();
      if (now - lastScrollSample < 34) return;
      lastScrollSample = now;
      requestDraw();
    }, { passive: true, capture: true });

    let lastPointerSample = 0;
    document.addEventListener('pointermove', (event) => {
      const now = performance.now();
      if (now - lastPointerSample < 34) return;
      lastPointerSample = now;
      if (
        event.pointerType === 'touch'
        || reduceMotionQuery.matches
        || !isRenderActive()
      ) {
        return;
      }
      const target = event.target?.closest?.(
        '.liquid-webgl-target'
      );
      if (!target) {
        setPointerInactive();
        return;
      }
      const previousTarget = pointerTarget;
      pointerX = event.clientX;
      pointerY = event.clientY;
      pointerActive = true;
      pointerTarget = target;
      requestPointerDraw(previousTarget, target);
    }, { passive: true });

    window.addEventListener('pointerout', (event) => {
      if (!event.relatedTarget) setPointerInactive();
    }, { passive: true });
    window.addEventListener('blur', setPointerInactive);

    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) {
        requestDraw();
        scheduleWeatherAnimation();
      } else if (weatherRaf) {
        cancelAnimationFrame(weatherRaf);
        weatherRaf = 0;
      }
    });

    window.addEventListener('daxigua:weather-frame', (event) => {
      if (event.detail?.phase === 'transitioning' || weatherAnimationActive()) {
        scheduleWeatherAnimation();
      } else {
        requestDraw();
      }
    });
    window.addEventListener('daxigua:weather-change', () => {
      weatherLastDrawAt = 0;
      requestDraw();
      scheduleWeatherAnimation();
    });
    window.addEventListener('daxigua:weather-blur-change', () => {
      weatherLastDrawAt = 0;
      requestDraw();
    });
    window.addEventListener('daxigua:glass-targets-change', () => {
      refreshTargets();
      requestDraw();
    });
    reduceMotionQuery.addEventListener?.('change', () => {
      weatherLastDrawAt = 0;
      requestDraw();
      scheduleWeatherAnimation();
    });

    mutationObserver = new MutationObserver((records) => {
      pointerActive = false;
      pointerTarget = null;
      if (records.some((record) => record.attributeName === 'data-active-view')) {
        uiMotionUntil = performance.now() + 780;
      }
      refreshTargets();
      requestDraw();
    });
    mutationObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-active-view', 'data-glass-mode', 'data-ui-mode', 'data-weather'],
    });

    domMutationObserver = new MutationObserver((records) => {
      if (!records.some(mutationTouchesGlassTarget)) return;
      refreshTargets();
      requestDraw();
    });
    domMutationObserver.observe(document.body, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ['hidden', 'open', 'aria-hidden'],
    });

    if ('ResizeObserver' in window) {
      resizeObserver = new ResizeObserver(requestDraw);
      refreshTargets();
    }
  }

  function initializeBuffers() {
    program = createProgram();
    uniforms = locations();
    buffer = gl.createBuffer();
    prismProgram = createPrismProgram();
    prismLocations = prismLocationMap();
    prismBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([
        -1, -1,
         1, -1,
        -1,  1,
         1,  1,
      ]),
      gl.STATIC_DRAW
    );
  }

  function initGL() {
    canvas = document.createElement('canvas');
    canvas.id = 'liquid-glass-canvas';
    canvas.setAttribute('aria-hidden', 'true');
    document.body.insertBefore(
      canvas,
      document.body.firstChild
    );

    gl = canvas.getContext('webgl', {
      alpha: true,
      antialias: false,
      depth: false,
      stencil: false,
      preserveDrawingBuffer: false,
      powerPreference: isMobile() ? 'low-power' : 'high-performance',
      premultipliedAlpha: false,
    });
    if (!gl) throw new Error('WebGL unavailable');
    try {
      if ('unpackColorSpace' in gl) gl.unpackColorSpace = 'srgb';
      if ('drawingBufferColorSpace' in gl) gl.drawingBufferColorSpace = 'srgb';
    } catch (error) {
      // Older Safari/WebViews expose neither property. RGBA upload below is
      // still the compatibility fix; color-space hints are best-effort only.
    }

    initializeBuffers();

    canvas.addEventListener('webglcontextlost', (event) => {
      event.preventDefault();
      contextLost = true;
      pointerActive = false;
        document.documentElement.classList.remove(
        'webgl-glass-ready',
        'gpu-glass-ready'
      );
      delete document.documentElement.dataset.glassRenderer;
    });
    canvas.addEventListener('webglcontextrestored', () => {
      try {
        contextLost = false;
        disabled = false;
        try {
          if ('unpackColorSpace' in gl) gl.unpackColorSpace = 'srgb';
          if ('drawingBufferColorSpace' in gl) gl.drawingBufferColorSpace = 'srgb';
        } catch (error) {
          // See initGL: the RGBA texture path remains the reliable fallback.
        }
        initializeBuffers();
        texture = image ? createTexture(image) : null;
        rainForegroundTexture = rainForegroundImage
          ? createTexture(rainForegroundImage)
          : null;
        rainBackgroundTexture = rainBackgroundImage
          ? createTexture(rainBackgroundImage)
          : null;
        waterTexture = null;
        waterTextureWidth = 0;
        waterTextureHeight = 0;
        lastUploadedWaterFrame = -1;
        canvas.style.display = 'block';
        requestDraw();
        scheduleWeatherAnimation();
      } catch (error) {
        console.warn(
          '[CrystalClear] context restore failed',
          error
        );
        fallback();
      }
    });
  }

  let started = false;

  async function start(forceWebGL = false) {
    if (started) return;
    if (!forceWebGL) {
      try {
        const webgpuReady = await window.JTYLiquidGlassWebGPU?.ready;
        if (webgpuReady) return;
      } catch (error) {
        console.warn('[CrystalClear] WebGPU handoff failed; trying WebGL', error);
      }
    }
    started = true;
    try {
      initGL();
      refreshTargets();
      bindEvents();
      document.fonts?.ready
        ?.then(requestDraw)
        .catch(() => {});
      requestDraw();
      scheduleWeatherAnimation();
      window.setTimeout(requestDraw, 180);
      window.setTimeout(requestDraw, 520);
    } catch (error) {
      console.warn(
        '[CrystalClear] WebGL disabled; GPU glass unavailable',
        error
      );
      fallback();
    }
  }

  window.JTYLiquidGlassFallbackToWebGL = () => start(true);

  if (document.readyState === 'loading') {
    document.addEventListener(
      'DOMContentLoaded',
      start,
      { once: true }
    );
  } else {
    start();
  }
})();
