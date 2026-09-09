import { BUILTIN_THEMES, layoutById, PALETTE } from "../layouts/registry";
import type { CustomLayout, LayoutId, ThemePreset } from "../types";
import type { Ctor, StoreCoreBase } from "./core.svelte";

/**
 * Appearance (Config → Themes): the active layout, selectable colour schemes
 * (built-in, user presets, plugin contributions), their localStorage
 * persistence, and the <html> repaint `applyAppearance` performs.
 *
 * Seams on `StoreCoreBase`: reads `plugins` so `pluginThemes` can derive the
 * plugin-contributed schemes; exposes `applyAppearance` /
 * `reconcilePluginTheme` to the plugins concern, `restoreAppearance` /
 * `refreshLayouts` to the composition root's constructor and bootstrap.
 */

/** #rrggbb (or 3-digit) hex → "r, g, b" triplet for rgba(var(--glow-*), a). */
function hexToTriplet(hex: string): string {
  let h = hex.replace("#", "").trim();
  if (h.length === 3) h = h.split("").map((c) => c + c).join("");
  const n = parseInt(h, 16);
  if (Number.isNaN(n) || h.length !== 6) return hex;
  return `${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}`;
}

/** Glassmorphism FX: translucent surfaces + backdrop blur, riding on top of
 *  whichever scheme is active (so it survives layout/theme switches). */
interface GlassFX {
  enabled: boolean;
  /** backdrop blur radius for the chrome (dock, bars, composer) in px (0–32). */
  blur: number;
  /** chrome transparency as a percentage (0–60). */
  tint: number;
  /** backdrop blur radius for panels/windows (messages, state rows, modals) in px (0–32). */
  panelBlur: number;
  /** panel/window transparency as a percentage (0–60). */
  panelTint: number;
}

/** Ambient background FX: full-viewport gradient blobs behind the shell. */
interface BgFX {
  enabled: boolean;
  /** layer opacity, 0–100 (ambient-gradient fallback). */
  intensity: number;
  /** explicit accent hexes; empty = follow the active scheme's glow colors. */
  c1: string;
  c2: string;
  /** wallpaper image as a data URL; empty = use the ambient gradient. */
  wallpaper: string;
  /** blur applied to the wallpaper image itself, in px (0–20). */
  wallBlur: number;
}

/** Clamp a recovered persisted number, falling back when it's not a number (so
 *  a hand-edited/garbage localStorage value can't break the UI). */
function clampNum(v: unknown, min: number, max: number, fallback: number): number {
  const n = typeof v === "number" && Number.isFinite(v) ? v : Number.NaN;
  return Number.isNaN(n) ? fallback : Math.min(max, Math.max(min, n));
}

export function AppearanceMixin<T extends Ctor<StoreCoreBase>>(Base: T) {
  return class Appearance extends Base {
    // ---- appearance (Config → Themes): layout + compatible color schemes ----
    layout = $state<LayoutId>("default");
    theme = $state("arcane");

    customLayouts = $state<CustomLayout[]>([]);
    customThemes = $state<ThemePreset[]>([]);
    activeCustomLayout = $state<string | null>(null);
/** Glass + ambient-background FX (Config → Themes). Persisted user toggles
*  that ride on top of whichever layout/theme is active, so they survive
*  scheme and layout switches. */
glass = $state<GlassFX>({ enabled: false, blur: 16, tint: 30, panelBlur: 12, panelTint: 20 });
bgfx = $state<BgFX>({ enabled: false, intensity: 55, c1: "", c2: "", wallpaper: "", wallBlur: 0 });

    /** Built-in + user presets. Plugin-contributed schemes are NOT in here: they
     *  live in `pluginThemes`, so they never reach `persistThemes` (a plugin owns
     *  its scheme; copying it into localStorage would fossilise an old version)
     *  and can't be renamed or deleted. Read `allThemes` to see everything. */
    themes = $state<ThemePreset[]>(BUILTIN_THEMES);
    private readonly layoutKey = "xu.layout";
    private readonly themeKey = "xu.theme";
    private readonly themesKey = "xu.themes";
private readonly glassKey = "xu.glass";
private readonly bgfxKey = "xu.bgfx";
private readonly wallpaperKey = "xu.wallpaper";
/** Last wallpaper string actually written, so slider ticks skip the big write. */
private wallpaperWritten: string | null = null;

    /** Schemes contributed by enabled plugins, flattened out of `plugin.list`.
     *  Ids are namespaced `plugin:<plugin>:<id>` so two plugins can both ship a
     *  "midnight" without colliding, and so a persisted id is recognisable as
     *  plugin-owned before the plugin list has arrived. */
    pluginThemes = $derived(
      this.plugins.flatMap((p) =>
        p.enabled && p.themes?.length
        ? p.themes.map((t) => ({
              id: `plugin:${p.name}:${t.id}`,
              name: t.name,
              builtin: false,
              plugin: p.name,
              css: t.css,
              layout: (t.layout ?? "default") as LayoutId,
              colors: t.colors ?? {},
        }))
        : [],
      ),
    );

    /** Every selectable scheme: built-ins, user presets, plugin contributions. */
    allThemes = $derived<ThemePreset[]>([...this.themes, ...this.pluginThemes]);

    private persistThemes(): void {
      try {
        localStorage.setItem(this.themesKey, JSON.stringify(this.themes.filter((t) => !t.builtin)));
      } catch { /* storage can be unavailable */ }
    }

    /** Custom properties written by the last applyAppearance. A plugin scheme may
     *  set vars outside PALETTE, so clearing only PALETTE would leak them into the
     *  next scheme. */
    private appliedVars: string[] = [];

    protected applyAppearance(): void {
      const el = typeof document !== "undefined" ? document.documentElement : null;
      if (!el) return;
      for (const c of Array.from(el.classList)) {
        if (c.startsWith("theme-") || c.startsWith("layout-")) el.classList.remove(c);
      }
      for (const p of PALETTE) el.style.removeProperty(`--${p.var}`);
      for (const v of this.appliedVars) el.style.removeProperty(v);
      this.appliedVars = [];
      // `plugin:<name>` would make a class containing a colon — legal in
      // classList but needing an escape in every selector targeting it. Normalise
      // so a plugin can style itself with a plain `.layout-plugin-<name>`.
      el.classList.add(`layout-${this.layout.replace(":", "-")}`);
      const t = this.allThemes.find((x) => x.id === this.theme && (x.layout ?? "default") === this.layout);
      this.applyThemeCss(t ?? null);
      if (t) {
        if (t.builtin) el.classList.add(`theme-${t.id}`);
        else if (t.plugin) {
          // A plugin scheme is not limited to PALETTE: it may set any var its own
          // stylesheet reads. Keys and values were validated brain-side, so the
          // only risk left is leaking on switch-away — hence appliedVars.
          el.classList.add(`theme-plugin-${t.plugin}`);
          for (const [key, value] of Object.entries(t.colors)) {
            const name = `--${key}`;
            el.style.setProperty(name, key.startsWith("glow-") ? hexToTriplet(value) : value);
            this.appliedVars.push(name);
          }
        } else for (const p of PALETTE) {
          const v = t.colors[p.var];
          if (v) el.style.setProperty(`--${p.var}`, p.var.startsWith("glow-") ? hexToTriplet(v) : v);
        }
      }
      this.applyFx();
    }


    /** Ambient background + glass are FX that ride on top of the active scheme:
     *  toggle the two classes and set the handful of CSS vars they read, without
     *  re-running the full theme repaint (cheap enough for a live slider). */
    private applyFx(): void {
      const el = typeof document !== "undefined" ? document.documentElement : null;
      const body = typeof document !== "undefined" ? document.body : null;
      if (!el) return;
      el.classList.toggle("glass-on", this.glass.enabled);
      const wall = this.bgfx.enabled && this.bgfx.wallpaper;
      body?.classList.toggle("bgfx-on", this.bgfx.enabled);
      body?.classList.toggle("bgfx-wall", !!wall);
      if (wall) el.style.setProperty("--bgfx-wall", `url("${wall}")`);
      el.style.setProperty("--glass-blur", `${this.glass.blur}px`);
      // The keep-vars are the opaque fraction surfaces keep (100 − transparency).
      el.style.setProperty("--glass-keep", `${100 - this.glass.tint}%`);
      // Panels/windows (messages, state rows, modals) get their own dials, so
      // chrome and content glass can be tuned independently.
      el.style.setProperty("--glass-panel-blur", `${this.glass.panelBlur}px`);
      el.style.setProperty("--glass-panel-keep", `${100 - this.glass.panelTint}%`);
      // Wallpaper's own blur softens the image itself.
      el.style.setProperty("--bgfx-wall-blur", `${this.bgfx.wallBlur}px`);
      // Background accents: an explicit override, else the active scheme's glow
      // colors, so the layer recolors automatically on a theme switch.
      const t = this.allThemes.find((x) => x.id === this.theme && (x.layout ?? "default") === this.layout);
      el.style.setProperty("--bgfx-c1", this.bgfx.c1 || t?.colors["glow-mag"] || "#8bc7ff");
      el.style.setProperty("--bgfx-c2", this.bgfx.c2 || t?.colors["glow-cyan"] || "#5fd4ff");
      el.style.setProperty("--bgfx-intensity", `${this.bgfx.intensity / 100}`);
    }

    setGlass(patch: Partial<GlassFX>): void {
      this.glass = { ...this.glass, ...patch };
      this.persistGlass();
      this.applyFx();
    }

    setBgfx(patch: Partial<BgFX>): void {
      this.bgfx = { ...this.bgfx, ...patch };
      this.persistBgfx();
      this.applyFx();
    }

    private persistGlass(): void {
      try { localStorage.setItem(this.glassKey, JSON.stringify(this.glass)); } catch { /* storage can be unavailable */ }
    }

    /** The wallpaper data URL is by far the biggest string here, so it lives in
     *  its own key and is only rewritten when it actually changes. Without this
     *  a slider drag fires dozens of setBgfx ticks a second, each re-writing
     *  megabytes to localStorage — the UI visibly lags behind the thumb. */
    private persistBgfx(): void {
      try {
        const { wallpaper, ...small } = this.bgfx;
        localStorage.setItem(this.bgfxKey, JSON.stringify(small));
        if (wallpaper === this.wallpaperWritten) return;
        if (wallpaper) localStorage.setItem(this.wallpaperKey, wallpaper);
        else localStorage.removeItem(this.wallpaperKey);
        this.wallpaperWritten = wallpaper;
      } catch { /* storage can be unavailable */ }
    }
    /** Swap the one `<link>` carrying a plugin scheme's extra stylesheet.
     *  Only an active plugin theme with a `css` file gets one; everything else
     *  removes it, so a stale sheet can never outlive the scheme that asked for
     *  it. The href points at the brain's plugin route, which serves the file
     *  only while that plugin is enabled. */
    private applyThemeCss(t: ThemePreset | null): void {
      if (typeof document === "undefined") return;
      const id = "xu-plugin-theme-css";
      const existing = document.getElementById(id);
      if (!t?.plugin || !t.css) {
        existing?.remove();
        return;
      }
      const href = `/plugins/${encodeURIComponent(t.plugin)}/${encodeURIComponent(t.css)}`;
      if (existing instanceof HTMLLinkElement) {
        if (existing.getAttribute("href") !== href) existing.setAttribute("href", href);
        return;
      }
      const link = document.createElement("link");
      link.id = id;
      link.rel = "stylesheet";
      link.href = href;
      document.head.appendChild(link);
    }

    /** A layout or color-scheme switch is "pending apply": the live repaint
     *  is imperfect and a cold-start reload perfects it. Switching stashes a
     *  return target and schedules that reload automatically; the Refresh
     *  button (applyNow) forces it immediately. In-memory so a fresh load
     *  starts clean. */
    pendingApply = $state(false);

    private reloadTimer: ReturnType<typeof setTimeout> | undefined;

    /** Stash where the post-reload boot should land, then schedule ONE
     *  debounced reload so flipping through presets doesn't reload-spam. */
    private scheduleReload(): void {
      this.pendingApply = true;
      if (typeof window === "undefined") return;
      try { sessionStorage.setItem("cfg-return", "themes"); } catch { /* storage unavailable */ }
      clearTimeout(this.reloadTimer);
      this.reloadTimer = setTimeout(() => window.location.reload(), 600);
    }

    setLayout(id: LayoutId): void {
      // A plugin layout paints from its own stylesheet, so it has no built-in scheme
      // to inherit and the compatible-scheme guard below would reject it outright.
      // It may still have a plugin-contributed scheme aimed at it.
      if (id.startsWith("plugin:")) {
        this.layout = id;
        const own = this.pluginThemes.find((t) => (t.layout ?? "default") === id);
        if (own) this.theme = own.id;
        try {
          localStorage.setItem(this.layoutKey, id);
          if (own) localStorage.setItem(this.themeKey, own.id);
        }
        catch { /* storage can be unavailable */ }
        this.applyAppearance();
        this.scheduleReload();
        return;
      }
      // `allThemes`, not `themes`: a plugin may be the only source of a scheme for
      // this layout. Built-ins come first in that list, so they still win by default.
      if (!this.allThemes.some((t) => (t.layout ?? "default") === id)) return;
      // Persist, adopt a scheme compatible with the chosen layout, repaint the
      // live tab, then auto-reload to perfect it (lands back on Themes).
      const compatibleTheme = this.allThemes.find((t) => (t.layout ?? "default") === id)?.id ?? "arcane";
      this.layout = id;
      this.theme = compatibleTheme;
      try { localStorage.setItem(this.layoutKey, id); localStorage.setItem(this.themeKey, compatibleTheme); }
      catch { /* storage can be unavailable */ }
      this.applyAppearance();
      this.scheduleReload();
    }
    setCustomLayout(id: string): void {
      const custom = this.customLayouts.find((item) => `custom:${item.name}` === id);
      if (!custom) return;
      this.activeCustomLayout = custom.name;
      this.layout = custom.base;
      const compatible = this.themes.find((item) => item.id === `custom:${custom.name}` || (item.layout ?? "default") === custom.base);
      if (compatible) this.theme = compatible.id;
      try {
        localStorage.setItem(this.layoutKey, this.layout);
        localStorage.setItem(this.themeKey, this.theme);
      } catch { /* storage can be unavailable */ }
      this.applyAppearance();
      this.scheduleReload();
    }

    setTheme(id: string): void {
      // `allThemes`: a plugin-contributed scheme is selectable like any other.
      const t = this.allThemes.find((x) => x.id === id && (x.layout ?? "default") === this.layout);
      if (!t || id === this.theme) return;
      this.theme = id;
      try { localStorage.setItem(this.themeKey, id); } catch { /* storage can be unavailable */ }
      this.applyAppearance();
      // Live repaint is imperfect; a cold-start reload perfects it. Auto-fires
      // shortly and lands back on the themes menu.
      this.scheduleReload();
    }

    /** Reload now to perfect the live-applied appearance change. */
    applyNow(): void {
      if (typeof window === "undefined") return;
      try { sessionStorage.setItem("cfg-return", "themes"); } catch { /* storage unavailable */ }
      window.location.reload();
    }

    /** New preset seeded from the active scheme's colors; activates + persists. */
    createTheme(): string {
      // `allThemes`: seeding a user preset from a plugin scheme is the natural way
      // to tweak one, and a plugin scheme is otherwise not editable.
      const base = this.allThemes.find((t) => t.id === this.theme && (t.layout ?? "default") === this.layout) ?? BUILTIN_THEMES[0];
      const n = this.themes.filter((t) => !t.builtin).length + 1;
      // `plugin`/`css` are deliberately dropped: the copy is a user preset, and
      // carrying the plugin's stylesheet would break the moment it is disabled.
      const preset: ThemePreset = { id: `custom-${Date.now().toString(36)}`, name: `CUSTOM ${n}`, builtin: false, layout: this.layout, colors: { ...base.colors } };
      this.themes = [...this.themes, preset];
      this.theme = preset.id;
      this.persistThemes();
      try { localStorage.setItem(this.themeKey, preset.id); } catch { /* storage can be unavailable */ }
      this.applyAppearance();
      return preset.id;
    }

    renameTheme(id: string, name: string): void {
      const t = this.themes.find((x) => x.id === id);
      if (!t || t.builtin) return;
      const clean = name.trim().slice(0, 24);
      if (clean) t.name = clean;
      this.persistThemes();
    }

    updateThemeColors(id: string, colors: Record<string, string>): void {
      const t = this.themes.find((x) => x.id === id);
      if (!t || t.builtin) return;
      const valid: Record<string, string> = {};
      for (const [k, v] of Object.entries(colors)) {
        if (/^#[0-9a-f]{6}$/i.test(v)) valid[k] = v;
      }
      if (Object.keys(valid).length === 0) return;
      t.colors = { ...t.colors, ...valid };
      this.persistThemes();
      if (this.theme === id) this.applyAppearance();
    }

    deleteTheme(id: string): boolean {
      const t = this.themes.find((x) => x.id === id);
      if (!t || t.builtin) return false;
      if (typeof window !== "undefined" && !window.confirm(`Remove theme “${t.name}”? Its custom layout and color scheme will be deleted.`)) return false;
      this.themes = this.themes.filter((x) => x.id !== id);
      this.persistThemes();
      if (this.theme === id) {
        const fallback = this.themes.find((x) => (x.layout ?? "default") === this.layout && x.builtin) ?? BUILTIN_THEMES[0];
        this.theme = fallback.id;
        this.layout = fallback.layout ?? "default";
        try { localStorage.setItem(this.themeKey, this.theme); localStorage.setItem(this.layoutKey, this.layout); } catch { /* storage can be unavailable */ }
      }
      this.applyAppearance();
      return true;
    }

    /**
     *  Plugin schemes only exist once this list lands, so a persisted
     *  `plugin:<plugin>:<id>` selection is unresolvable before it — the
     *  constructor keeps such an id on faith, and this is where it either becomes
     *  real (repaint with its colours and stylesheet) or is found gone (plugin
     *  disabled or uninstalled) and replaced with a compatible built-in. Without
     *  the fallback the shell would sit on an id nothing matches, painting the
     *  default palette with no scheme selected anywhere in Config.
     */
    protected reconcilePluginTheme(): void {
      if (this.theme.startsWith("plugin:") && !this.pluginThemes.some((t) => t.id === this.theme)) {
        const fallback = this.themes.find((t) => (t.layout ?? "default") === this.layout && t.builtin);
        this.theme = fallback?.id ?? BUILTIN_THEMES[0].id;
        try { localStorage.setItem(this.themeKey, this.theme); } catch { /* storage can be unavailable */ }
      }
    }

    /** Cold-start restore from localStorage: custom presets, saved
     *  layout/scheme ids (a `plugin:*` id is kept on faith until the plugin
     *  list lands), and seeding any built-ins added since the last visit.
     *  Called once by the composition root's constructor after every mixin
     *  field is initialised. */
    protected restoreAppearance(): void {
      if (typeof localStorage !== "undefined") {
        try {
          const raw = localStorage.getItem(this.themesKey);
          if (raw) {
            const parsed: unknown = JSON.parse(raw);
            if (Array.isArray(parsed)) {
              const customs = parsed.filter(
                (t): t is ThemePreset =>
                !!t && typeof t === "object" &&
                typeof (t as ThemePreset).id === "string" &&
                typeof (t as ThemePreset).name === "string" &&
                !(t as ThemePreset).builtin &&
                !!((t as ThemePreset).colors),
              );
              if (customs.length > 0) this.themes = [...BUILTIN_THEMES, ...customs.map((t) => ({ ...t, layout: t.layout ?? "default" }))];
            }
          }
          const savedLayout = localStorage.getItem(this.layoutKey);
          // `plugin:*` is accepted on sight: the plugin list arrives later over
          // the socket, so validating against it here would always fail and drop
          // the user back to a built-in layout on every cold start. A stale id
          // that no registered layout matches (removed in an earlier release)
          // fails the check, so `this.layout` keeps its default — the app never
          // tries to load a deleted layout chunk.
          if (savedLayout && layoutById(savedLayout)) this.layout = savedLayout;
          else if (savedLayout?.startsWith("plugin:")) this.layout = savedLayout;
          const savedTheme = localStorage.getItem(this.themeKey);
          // A `plugin:*` scheme id is accepted on sight for the same reason as the
          // layout above: the plugin list arrives later over the socket, so
          // validating now would always fail and silently reset the user's choice.
          // `refreshPlugins` reconciles it once the list lands.
          if (savedTheme?.startsWith("plugin:")) this.theme = savedTheme;
          else if (savedTheme && this.themes.some((t) => t.id === savedTheme)) this.theme = savedTheme;
          // Safety net: a stale persisted scheme for a theme removed in an
          // earlier release (absent from `this.themes`) was never set above, and
          // a scheme whose layout no longer matches the possibly migrated layout
          // also fails this check — in either case fall back to the active
          // layout's scheme, ultimately `arcane`, so the app is never left
          // unstyled.
          if (!this.theme.startsWith("plugin:") && !this.themes.some((t) => t.id === this.theme && (t.layout ?? "default") === this.layout)) {
            this.theme = this.themes.find((t) => (t.layout ?? "default") === this.layout)?.id ?? "arcane";
          }
        } catch { /* corrupt browser state is ignored */ }
        try {
          const g = localStorage.getItem(this.glassKey);
          if (g) {
            const v: unknown = JSON.parse(g);
            if (v && typeof v === "object") {
              const o = v as Partial<GlassFX>;
              this.glass = { enabled: !!o.enabled, blur: clampNum(o.blur, 0, 32, 16), tint: clampNum(o.tint, 0, 60, 30), panelBlur: clampNum(o.panelBlur, 0, 32, 12), panelTint: clampNum(o.panelTint, 0, 60, 20) };
            }
          }
          const b = localStorage.getItem(this.bgfxKey);
          if (b) {
            const v: unknown = JSON.parse(b);
            if (v && typeof v === "object") {
              const o = v as Partial<BgFX>;
              // Legacy: the wallpaper used to live inside this blob; prefer the
              // dedicated key, fall back to the embedded one.
              const wall = localStorage.getItem(this.wallpaperKey) ?? (typeof o.wallpaper === "string" ? o.wallpaper : "");
              this.bgfx = {
                enabled: !!o.enabled,
                intensity: clampNum(o.intensity, 0, 100, 55),
                c1: typeof o.c1 === "string" ? o.c1 : "",
                c2: typeof o.c2 === "string" ? o.c2 : "",
                wallpaper: wall,
                wallBlur: clampNum(o.wallBlur, 0, 20, 0),
              };
              this.wallpaperWritten = wall;
            }
          }
        } catch { /* corrupt fx state is ignored */ }
      }

      // Seed any builtin themes missing from this.themes (idempotent; new builtins ship on existing installs).
      let seededBuiltin = false;
      for (const preset of BUILTIN_THEMES) {
        if (!this.themes.some((t) => t.id === preset.id)) {
          this.themes.push(structuredClone(preset));
          seededBuiltin = true;
        }
      }
      if (seededBuiltin) this.persistThemes();
      this.applyAppearance();
    }

    /** Fetch themeable-layout definitions (brain-side custom layouts/themes)
     *  and merge them into the selectable set. Called by the bootstrap. */
    protected async refreshLayouts(): Promise<void> {
      try {
        const got = await this.client.call<{ custom_layouts: CustomLayout[]; custom_themes: Array<{ name: string; base?: LayoutId; tokens?: Record<string, string> }>; active?: { layout?: string; theme?: string } }>("layouts.list");
        this.customLayouts = got.custom_layouts ?? [];
        this.customThemes = (got.custom_themes ?? []).map((t) => ({
              id: `custom:${t.name}`,
              name: t.name,
              builtin: false,
              layout: t.base ?? "default",
              colors: t.tokens ?? {},
        }));
        this.themes = [...this.themes.filter((t) => t.builtin), ...this.customThemes];
        const activeLayout = this.customLayouts.find((item) => item.name === got.active?.layout);
        if (activeLayout) {
          this.activeCustomLayout = activeLayout.name;
          this.layout = activeLayout.base;
        }
        const activeTheme = this.customThemes.find((item) => item.name === got.active?.theme);
        if (activeTheme && (activeTheme.layout ?? "default") === this.layout) this.theme = activeTheme.id;
        this.applyAppearance();
      } catch { /* layout definitions not ready */ }
    }
  };
}
