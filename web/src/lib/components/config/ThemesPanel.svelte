<script lang="ts">
  import { brain, type ThemePreset, type LayoutId } from "../../store.svelte";
  import { LAYOUTS as BUILTIN_LAYOUTS, paletteFor } from "../../layouts/registry";

  let { activeModule = "themes" }: { activeModule?: string } = $props();

  const activeCustom = $derived(brain.themes.find((t) => !t.builtin && t.id === brain.theme) ?? null);

  /** Built-in layout arrangements come from the layout registry — each layout
   *  folder declares its own Config label and sublabel, so adding one never
   *  means editing this list. */
  const LAYOUTS: { id: LayoutId; name: string; desc: string }[] = BUILTIN_LAYOUTS.map(
    ({ id, name, desc }) => ({ id, name, desc }),
  );
const CUSTOM_LAYOUTS = $derived(brain.customLayouts.map((item) => ({
    id: `custom:${item.name}` as LayoutId,
    name: item.name.toUpperCase(),
    desc: `${item.base} · ${item.sidebar ?? "left"} navigation · ${item.density ?? "comfortable"}`,
  })));
  /** Layouts contributed by plugins: an enabled plugin whose manifest declares
   *  `ui.mount: "layout"` replaces the whole shell. Listed here so such a
   *  plugin is selectable the same way a built-in is — otherwise it would be
   *  installable but unreachable. */
  const PLUGIN_LAYOUTS = $derived(
    brain.plugins
      .filter((p) => p.enabled && p.ui?.element && p.ui.mount === "layout")
      .map((p) => ({
        id: `plugin:${p.name}` as LayoutId,
        name: p.name.toUpperCase(),
        desc: p.description || "plugin layout · ships its own color scheme",
      })),
  );
  const ALL_LAYOUTS = $derived([...LAYOUTS, ...CUSTOM_LAYOUTS, ...PLUGIN_LAYOUTS]);
  /** Color schemes compatible with the active layout. Each preset is tagged
   *  with the layout it belongs to; untagged presets default to `default`.
   *  `allThemes` so plugin-contributed schemes appear beside the built-ins. */
  const compatibleThemes = $derived(
    brain.allThemes.filter((t) => (t.layout ?? "default") === brain.layout),
  );

  /** Deleting a preset is not undoable, so it takes two clicks: the first arms
   *  this card (which disarms any other), the second commits. The timer drops
   *  the arm so a stray first click cannot sit there waiting to be triggered. */
  let delArm = $state<string | null>(null);
  let armTimer: ReturnType<typeof setTimeout> | undefined;
  function armDelete(id: string): void {
    delArm = id;
    clearTimeout(armTimer);
    armTimer = setTimeout(() => { delArm = null; }, 3000);
  }
  async function confirmDelete(id: string): Promise<void> {
    clearTimeout(armTimer);
    delArm = null;
    await brain.deleteTheme(id);
  }

  function swatchOf(t: ThemePreset): string[] {
    return [t.colors.bg, t.colors.surface, t.colors.magenta, t.colors.cyan];
  }

  /** Effective ambient accent colors: the FX layer uses the active scheme's
   *  glow colors unless the user set an explicit override. Shown as the color
   *  pickers' value so they don't sit on a blank swatch before first use. */
  const fxAccents = $derived.by(() => {
    const t = brain.allThemes.find((x) => x.id === brain.theme && (x.layout ?? "default") === brain.layout);
    return {
      c1: t?.colors["glow-mag"] || "#8bc7ff",
      c2: t?.colors["glow-cyan"] || "#5fd4ff",
    };
  });

  /** Read a chosen image file, downscale it on an offscreen canvas so a big
   *  wallpaper fits comfortably in localStorage, and hand back a JPEG data URL. */
  function downscaleWallpaper(file: Blob, maxDim: number): Promise<string> {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => {
        try {
          const scale = Math.min(1, maxDim / Math.max(img.width, img.height));
          const w = Math.max(1, Math.round(img.width * scale));
          const h = Math.max(1, Math.round(img.height * scale));
          const canvas = document.createElement("canvas");
          canvas.width = w;
          canvas.height = h;
          const ctx = canvas.getContext("2d");
          if (!ctx) throw new Error("no 2d context");
          ctx.drawImage(img, 0, 0, w, h);
          resolve(canvas.toDataURL("image/jpeg", 0.82));
        } catch (err) {
          reject(err);
        } finally {
          URL.revokeObjectURL(url);
        }
      };
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("could not decode image")); };
      img.src = url;
    });
  }

  async function pickWallpaper(e: Event): Promise<void> {
    const input = e.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    input.value = "";
    if (!file) return;
    if (!file.type.startsWith("image/")) { alert("Pick an image file (PNG/JPEG/WebP)."); return; }
    try {
      brain.setBgfx({ wallpaper: await downscaleWallpaper(file, 2400) });
    } catch {
      alert("That image could not be read.");
    }
  }

  function themeKind(t: ThemePreset): string {
    if (t.builtin) return "built-in scheme";
    return t.plugin ? `from ${t.plugin}` : "custom preset";
  }
</script>

<!-- ========== THEMES ========== -->
<div class="cfg-pane" class:active={activeModule === "themes"}>
  <div class="cfg-sec">
    <div class="cfg-sec-hd">
      <span class="t">LAYOUT STYLE</span>
      <span class="d">frontend arrangement · decides which color schemes are on offer</span>
      <span class="sp"></span>
    </div>
    <div class="theme-grid">
      {#each ALL_LAYOUTS as item (item.id)}
        <button type="button" class="theme-card" class:active={brain.layout === item.id || (item.id.startsWith("custom:") && brain.activeCustomLayout === item.id.slice(7))} onclick={() => item.id.startsWith("custom:") ? brain.setCustomLayout(item.id) : brain.setLayout(item.id)} aria-pressed={brain.layout === item.id}>
          <div class="theme-name">{item.name}</div><div class="theme-desc">{item.desc}</div>
          {#if brain.layout === item.id}<span class="theme-check">ACTIVE</span>{/if}
        </button>
      {/each}
    </div>
  </div>

  <div class="cfg-sec">
    <div class="cfg-sec-hd">
      <span class="t">COLOR SCHEME</span>
      <span class="d">only {brain.layout.toUpperCase()} schemes are shown · a new preset starts from the active one</span>
      <span class="sp"></span>
      <button type="button" class="k-btn ghost" onclick={() => void brain.createTheme()}>+ New preset</button>
    </div>
    {#if brain.layout.startsWith("plugin:") && compatibleThemes.length === 0}
      <!-- A plugin layout paints from its own stylesheet, and no built-in
           preset targets it. It may still ship its own schemes — this only
           shows when it ships none, where an empty grid would look broken. -->
      <div class="k-empty">This layout ships its own colors. Switch to a built-in layout to choose a scheme.</div>
    {/if}
    <div class="theme-grid">
      {#each compatibleThemes as t (t.id)}
        <div class="theme-cell">
          <button type="button" class="theme-card" class:active={brain.theme === t.id} onclick={() => brain.setTheme(t.id)} aria-pressed={brain.theme === t.id}>
            <div class="theme-swatch">{#each swatchOf(t) as c, i (i)}<span style="background:{c}"></span>{/each}</div>
            <div class="theme-name">{t.name}</div><div class="theme-desc">{themeKind(t)}</div>
            {#if brain.theme === t.id}<span class="theme-check">ACTIVE</span>{/if}
          </button>
          <!-- Only a user preset is deletable: a plugin owns its scheme, and
               removing it here would resurrect on the next plugin.list. -->
          {#if !t.builtin && !t.plugin}
            <button
              type="button"
              class="theme-del k-btn dgr icon"
              class:armed={delArm === t.id}
              title={delArm === t.id ? `click again to delete ${t.name}` : `delete ${t.name}`}
              aria-label={delArm === t.id ? `click again to delete ${t.name}` : `delete ${t.name}`}
              onclick={() => delArm === t.id ? void confirmDelete(t.id) : armDelete(t.id)}
            >×</button>
          {/if}
        </div>
      {/each}
    </div>
  </div>

  {#if activeCustom}
    <div class="cfg-sec">
      <div class="cfg-sec-hd">
        <span class="t">EDIT PRESET</span>
        <span class="d">name and colours apply instantly — the shell repaints as you drag</span>
        <span class="sp"></span>
      </div>
      <div class="cfg-row">
        <div class="label">name<small>how the preset is labelled in COLOR SCHEME</small></div>
        <div class="ctrl"><input type="text" value={activeCustom.name} oninput={(e) => void brain.renameTheme(activeCustom.id, e.currentTarget.value)} /></div>
      </div>
      <div class="color-grid">{#each paletteFor(brain.layout) as p (p.var)}<label class="color-field"><span class="color-label">{p.label}</span><input type="color" value={activeCustom.colors[p.var]} oninput={(e) => void brain.updateThemeColors(activeCustom.id, { [p.var]: e.currentTarget.value })} /><span class="color-hex">{activeCustom.colors[p.var]}</span></label>{/each}</div>
    </div>
  {/if}

  <div class="cfg-sec">
    <div class="cfg-sec-hd">
      <span class="t">GLASS &amp; BACKGROUND</span>
      <span class="d">ambient accents · ride on top of every layout &amp; scheme</span>
      <span class="sp"></span>
    </div>

    <div class="cfg-row">
      <div class="label">glass<small>translucent chrome with a backdrop blur</small></div>
      <div class="ctrl">
        <span class="state-badge">{brain.glass.enabled ? "ON" : "OFF"}</span>
        <label class="toggle">
          <input type="checkbox" aria-label="enable glass" checked={brain.glass.enabled} onchange={() => brain.setGlass({ enabled: !brain.glass.enabled })} />
          <span class="track"></span><span class="thumb"></span>
        </label>
      </div>
    </div>
    {#if brain.glass.enabled}
      <div class="cfg-row">
        <div class="label">chrome blur<small>dock, bars, composer — backdrop blur radius</small></div>
        <div class="ctrl">
          <span class="k-slide">
            <input type="range" aria-label="glass chrome blur" min="0" max="32" step="1" value={brain.glass.blur} oninput={(e) => brain.setGlass({ blur: e.currentTarget.valueAsNumber })} />
            <output>{brain.glass.blur}px</output>
          </span>
        </div>
      </div>
      <div class="cfg-row">
        <div class="label">chrome transparency<small>dock, bars, composer — higher = more see-through</small></div>
        <div class="ctrl">
          <span class="k-slide">
            <input type="range" aria-label="glass chrome tint" min="0" max="60" step="1" value={brain.glass.tint} oninput={(e) => brain.setGlass({ tint: e.currentTarget.valueAsNumber })} />
            <output>{brain.glass.tint}%</output>
          </span>
        </div>
      </div>
      <div class="cfg-row">
        <div class="label">panel blur<small>messages, state rows, windows — backdrop blur radius</small></div>
        <div class="ctrl">
          <span class="k-slide">
            <input type="range" aria-label="glass panel blur" min="0" max="32" step="1" value={brain.glass.panelBlur} oninput={(e) => brain.setGlass({ panelBlur: e.currentTarget.valueAsNumber })} />
            <output>{brain.glass.panelBlur}px</output>
          </span>
        </div>
      </div>
      <div class="cfg-row">
        <div class="label">panel transparency<small>messages, state rows, windows — higher = more see-through</small></div>
        <div class="ctrl">
          <span class="k-slide">
            <input type="range" aria-label="glass panel tint" min="0" max="60" step="1" value={brain.glass.panelTint} oninput={(e) => brain.setGlass({ panelTint: e.currentTarget.valueAsNumber })} />
            <output>{brain.glass.panelTint}%</output>
          </span>
        </div>
      </div>
    {/if}

    <div class="cfg-row">
      <div class="label">background<small>wallpaper image, or ambient gradient when none is set</small></div>
      <div class="ctrl">
        <span class="state-badge">{brain.bgfx.enabled ? "ON" : "OFF"}</span>
        <label class="toggle">
          <input type="checkbox" aria-label="enable background" checked={brain.bgfx.enabled} onchange={() => brain.setBgfx({ enabled: !brain.bgfx.enabled })} />
          <span class="track"></span><span class="thumb"></span>
        </label>
      </div>
    </div>
    {#if brain.bgfx.enabled}
      <div class="cfg-row">
        <div class="label">wallpaper<small>browse for an image file — it becomes the shell background</small></div>
        <div class="ctrl">
          {#if brain.bgfx.wallpaper}
            <img class="wall-thumb" src={brain.bgfx.wallpaper} alt="wallpaper preview" />
            <button type="button" class="k-btn dgr" onclick={() => brain.setBgfx({ wallpaper: "" })}>clear</button>
          {/if}
          <label class="k-btn pri">
            browse
            <input type="file" accept="image/png,image/jpeg,image/webp" onchange={(e) => void pickWallpaper(e)} style="display:none" />
          </label>
        </div>
      </div>
      {#if brain.bgfx.wallpaper}
        <div class="cfg-row">
          <div class="label">wallpaper blur<small>softens the image itself — easier reading behind text</small></div>
          <div class="ctrl">
            <span class="k-slide">
              <input type="range" aria-label="wallpaper blur" min="0" max="20" step="1" value={brain.bgfx.wallBlur} oninput={(e) => brain.setBgfx({ wallBlur: e.currentTarget.valueAsNumber })} />
              <output>{brain.bgfx.wallBlur}px</output>
            </span>
          </div>
        </div>
      {:else}
      <div class="cfg-row">
        <div class="label">intensity<small>layer opacity</small></div>
        <div class="ctrl">
          <span class="k-slide">
            <input type="range" aria-label="background intensity" min="5" max="100" step="1" value={brain.bgfx.intensity} oninput={(e) => brain.setBgfx({ intensity: e.currentTarget.valueAsNumber })} />
            <output>{brain.bgfx.intensity}%</output>
          </span>
        </div>
      </div>
      <div class="cfg-row">
        <div class="label">accent colors<small>blank = follow the active scheme</small></div>
        <div class="ctrl">
          <label class="color-field">
            <input type="color" aria-label="background accent one" value={brain.bgfx.c1 || fxAccents.c1} oninput={(e) => brain.setBgfx({ c1: e.currentTarget.value })} />
            <span class="color-hex">{brain.bgfx.c1 || "theme"}</span>
          </label>
          <label class="color-field">
            <input type="color" aria-label="background accent two" value={brain.bgfx.c2 || fxAccents.c2} oninput={(e) => brain.setBgfx({ c2: e.currentTarget.value })} />
            <span class="color-hex">{brain.bgfx.c2 || "theme"}</span>
          </label>
        </div>
      </div>
    {/if}
    {/if}
  </div>
</div>
