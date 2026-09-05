<script lang="ts">
  import { tick } from "svelte";
  import { brain } from "../../store.svelte";
  import Icon from "../Icon.svelte";
  import type { ProviderInfo } from "../../types";

  let { activeModule = "providers", setMsg, refresh }: { activeModule?: string; setMsg: (text: string, kind?: "ok" | "err" | "info") => void; refresh: () => Promise<void> } = $props();

  let form = $state({ type: "openai-compatible", name: "", base_url: "", api_key: "", models: "" });
  let showForm = $state(false);
  let saving = $state(false);
  let testing = $state<Record<string, boolean>>({});
  let deleting = $state<Record<string, boolean>>({});
  let delConfirm = $state<string | null>(null);
  let confirmTimer: ReturnType<typeof setTimeout> | undefined;

  // --- provider row editor ---
  let editId = $state<string | null>(null);
  let editForm = $state({ base_url: "", api_key: "", models: "" });

  /** The stored provider the open draft is diffed against, so "unsaved changes"
   *  only shows when the brain really holds something else. A blank api key
   *  field means "keep the stored key", so any typed key is a change. */
  const editP = $derived(brain.providers.find((p) => p.id === editId) ?? null);
  const urlDirty = $derived(editP !== null && editForm.base_url !== editP.base_url);
  const keyDirty = $derived(editForm.api_key !== "");
  const modelsDirty = $derived(editP !== null && splitModels(editForm.models).join(", ") !== (editP.models ?? []).join(", "));
  const editDirty = $derived(urlDirty || keyDirty || modelsDirty);
  /** The add form writes nothing until ADD & TEST — this is what it has staged. */
  const addStaged = $derived(form.name !== "" || form.base_url !== "" || form.api_key !== "" || form.models !== "");

  function openEdit(p: ProviderInfo): void {
    if (editId === p.id) {
      editId = null;
      return;
    }
    editId = p.id;
    resetEdit(p.id);
  }

  /** Re-read the draft from the stored provider. DISCARD calls it, and so does
   *  the post-save resync, so the bar stops claiming a change it already wrote. */
  function resetEdit(id: string): void {
    const p = brain.providers.find((x) => x.id === id);
    if (p) editForm = { base_url: p.base_url, api_key: "", models: (p.models ?? []).join(", ") };
  }

  async function saveEdit(id: string): Promise<void> {
    if (!editForm.base_url) return;
    saving = true;
    setMsg("");
    try {
      await brain.addProvider({
        id,
        base_url: editForm.base_url,
        api_key: editForm.api_key || undefined,
        models: editForm.models
          ? editForm.models.split(",").map((m) => m.trim()).filter(Boolean)
          : [],
      });
      await brain.testProvider(id);
      await refresh();
      resetEdit(id);
      setMsg("provider updated", "ok");
    } catch (e) {
      setMsg(`failed: ${e instanceof Error ? e.message : String(e)}`, "err");
    } finally {
      saving = false;
    }
  }

  function addModel(name: string): void {
    const n = name.trim();
    if (!n) return;
    const cur = splitModels(editForm.models);
    if (!cur.includes(n)) cur.push(n);
    editForm.models = cur.join(", ");
  }

  function removeModel(name: string): void {
    editForm.models = splitModels(editForm.models).filter((m) => m !== name).join(", ");
  }

  async function save(): Promise<void> {
    if (!form.base_url) return;
    saving = true;
    setMsg("");
    try {
      const id = await brain.addProvider({
        type: form.type,
        name: form.name || undefined,
        base_url: form.base_url,
        api_key: form.api_key || undefined,
        models: form.models
          ? form.models.split(",").map((m) => m.trim()).filter(Boolean)
          : [],
      });
      // Auto-test right after create so models populate immediately.
      await brain.testProvider(id);
      await refresh();
      form = { type: "openai-compatible", name: "", base_url: "", api_key: "", models: "" };
      showForm = false;
      setMsg("provider saved — testing connection", "ok");
    } catch (e) {
      setMsg(`failed: ${e instanceof Error ? e.message : String(e)}`, "err");
    } finally {
      saving = false;
    }
  }

  /** Two-click delete: the first click arms, the second deletes. Arming one
   *  disarms any other, and it falls back to safe after 3s. */
  function armDelete(id: string): void {
    delConfirm = delConfirm === id ? null : id;
    clearTimeout(confirmTimer);
    if (delConfirm !== null) confirmTimer = setTimeout(() => (delConfirm = null), 3000);
  }

  async function remove(id: string): Promise<void> {
    clearTimeout(confirmTimer);
    delConfirm = null;
    deleting = { ...deleting, [id]: true };
    try {
      await brain.deleteProvider(id);
      await refresh();
      setMsg("provider removed", "info");
    } catch (e) {
      setMsg(`failed: ${e instanceof Error ? e.message : String(e)}`, "err");
    } finally {
      deleting = { ...deleting, [id]: false };
    }
  }

  async function toggleEnabled(id: string, enabled: boolean): Promise<void> {
    try {
      await brain.setProviderEnabled(id, enabled);
      setMsg(`provider ${enabled ? "enabled" : "disabled"}`, "ok");
    } catch (e) {
      setMsg(`failed: ${e instanceof Error ? e.message : String(e)}`, "err");
    }
  }

  async function test(id: string): Promise<void> {
    // REFRESH refetches the model list. Adopt it into the open draft only when
    // models were not hand-edited, so one click never eats typed input.
    const adoptModels = editId === id && !modelsDirty;
    testing = { ...testing, [id]: true };
    setMsg("testing connection…", "info");
    try {
      const r = await brain.testProvider(id);
      if (r.ok) {
        await refresh();
        if (adoptModels) editForm.models = (brain.providers.find((x) => x.id === id)?.models ?? []).join(", ");
        const n = r.models?.length ?? 0;
        setMsg(
          n > 0 ? `ok · ${n} model${n === 1 ? "" : "s"} · ${r.latency_ms ?? 0}ms` : "ok · no models returned",
          n > 0 ? "ok" : "info",
        );
      } else {
        setMsg(r.error ?? "connection failed", "err");
      }
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e), "err");
    } finally {
      testing = { ...testing, [id]: false };
    }
  }

  let formEl = $state<HTMLElement | null>(null);
  async function pickType(type: string): Promise<void> {
    form.type = type;
    form.base_url = type === "anthropic-compatible" ? "https://api.anthropic.com" : "";
    form.api_key = "";
    form.models = "";
    showForm = true;
    await tick();
    // Explicit scroll of the config pane's own scroller — scrollIntoView breaks
    // iframe-embedded previews by scrolling the outer frame.
    const box = formEl?.closest(".view-inner");
    if (formEl && box) box.scrollTop += formEl.getBoundingClientRect().top - box.getBoundingClientRect().top - 24;
  }

  function splitModels(text: string): string[] {
    return text.split(",").map((m) => m.trim()).filter(Boolean);
  }
</script>

  <!-- ========== PROVIDERS ========== -->
  <div class="cfg-pane" class:active={activeModule === "providers"}>
    <div class="cfg-sec">
      <div class="cfg-sec-hd">
        <span class="t">PROVIDERS</span>
        <span class="d">{brain.providers.length} configured · the switch applies at once, the form stages until you save</span>
        <span class="sp"></span>
      </div>

      {#if brain.providers.length === 0}
        <div class="panel provider-card empty-list"><span class="t">No providers configured yet.</span></div>
      {/if}
      {#each brain.providers as p (p.id)}
        <div class="panel provider-card" class:open={editId === p.id}>
          <div class="pc-head">
            <button
              type="button"
              class="pc-open"
              aria-expanded={editId === p.id}
              onclick={() => openEdit(p)}
            >
              <span class="pc-idx">{p.type.replace("-compatible", "").toUpperCase()}</span>
              <span class="pc-name">{p.name || p.id.toUpperCase()}</span>
              <span class="pc-sub">{p.models?.length ?? 0} models</span>
            </button>
            <label class="prov-en">
              <input type="checkbox" checked={p.enabled} aria-label="enable {p.name || p.id}"
                onchange={() => void toggleEnabled(p.id, !p.enabled)} />
              <span class="en-track"></span><span class="en-thumb"></span>
            </label>
            <span class="key-badge" class:ok={p.key_set}>{p.key_set ? "key" : "no key"}</span>
            <span class="pc-caret"><Icon name={editId === p.id ? "chevron-down" : "chevron-right"} size={13} /></span>
          </div>
          {#if editId === p.id}
            <div class="p-form" role="presentation" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}>
              <div class="cfg-bar" class:on={editDirty}>
                <span class="st">{editDirty ? "unsaved changes" : "in sync with the brain"}</span>
                <span class="sp"></span>
                <button type="button" class="k-btn" disabled={!editDirty || saving} onclick={() => resetEdit(p.id)}>DISCARD</button>
                <button type="button" class="k-btn pri" disabled={saving || editForm.base_url === ""} onclick={() => void saveEdit(p.id)}>
                  {saving ? "SAVING…" : "SAVE & TEST"}
                </button>
              </div>
              <div class="cfg-row" class:dirty={urlDirty}>
                <div class="label">base url
                  <small>the endpoint requests go to</small>
                  <div class="diff">{p.base_url || "—"} → {editForm.base_url || "—"}</div>
                </div>
                <div class="ctrl">
                  <input type="url" bind:value={editForm.base_url} placeholder="http://localhost:11434/v1" />
                </div>
              </div>

              <div class="cfg-row" class:dirty={keyDirty}>
                <div class="label">api key
                  <small>{p.key_set ? "set — leave blank to keep the stored key" : "not set"}</small>
                  <div class="diff">stored key replaced on save</div>
                </div>
                <div class="ctrl">
                  <input type="password" bind:value={editForm.api_key} placeholder={p.key_set ? "•••••••• (unchanged)" : "sk-…"} />
                </div>
              </div>

              <div class="cfg-row block" class:dirty={modelsDirty}>
                <div class="label">models
                  <small>click × to drop one · SAVE &amp; TEST writes the list, then refetches it</small>
                  <div class="diff">{p.models?.length ?? 0} stored → {splitModels(editForm.models).length} staged</div>
                </div>
                <div class="ctrl">
                  {#if splitModels(editForm.models).length === 0}
                    <span class="k-empty">none — SAVE &amp; TEST fetches them from the endpoint</span>
                  {:else}
                    <span class="k-chips">
                      {#each splitModels(editForm.models) as m (m)}
                        <span class="k-chip">{m}<button type="button" aria-label="remove {m}" onclick={() => removeModel(m)}>×</button></span>
                      {/each}
                    </span>
                  {/if}
                </div>
              </div>

              <div class="cfg-row">
                <div class="label">add model
                  <small>type an id and press Enter — staged, not written</small>
                </div>
                <div class="ctrl">
                  <input
                    type="text"
                    placeholder="+ model id, press Enter"
                    onkeydown={(e) => { if (e.key === "Enter") { e.preventDefault(); addModel(e.currentTarget.value); e.currentTarget.value = ""; } }}
                  />
                </div>
              </div>

              <div class="cfg-row">
                <div class="label">connection
                  <small>re-test the stored settings and refetch the model list</small>
                </div>
                <div class="ctrl">
                  <button type="button" class="k-btn" disabled={testing[p.id]} onclick={() => void test(p.id)}>
                    {testing[p.id] ? "…" : "REFRESH"}
                  </button>
                </div>
              </div>

              <div class="cfg-row">
                <div class="label">delete provider
                  <small>removes the entry and its stored key · click twice to confirm</small>
                </div>
                <div class="ctrl">
                  <button
                    type="button"
                    class="k-btn dgr"
                    class:armed={delConfirm === p.id}
                    disabled={deleting[p.id]}
                    onclick={() => (delConfirm === p.id ? void remove(p.id) : armDelete(p.id))}
                  >
                    {deleting[p.id] ? "…" : delConfirm === p.id ? "confirm?" : "DELETE"}
                  </button>
                </div>
              </div>

            </div>
          {/if}
        </div>
      {/each}
    </div>

    <!-- add provider -->
    <div class="cfg-sec">
      <div class="cfg-sec-hd">
        <span class="t">ADD PROVIDER</span>
        <span class="d">pick a wire format, fill the form, then ADD &amp; TEST</span>
        <span class="sp"></span>
      </div>
      <div class="grid-2 add-grid">
        <div class="add-provider" role="button" tabindex="0"
          onclick={() => pickType("openai-compatible")}
          onkeydown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pickType("openai-compatible"); } }}>
          + Add OpenAI-compatible provider
        </div>
        <div class="add-provider" role="button" tabindex="0"
          onclick={() => pickType("anthropic-compatible")}
          onkeydown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pickType("anthropic-compatible"); } }}>
          + Add Anthropic-compatible provider
        </div>
      </div>
      {#if showForm}
      <div class="panel add-provider-form" bind:this={formEl}>
        <h3>{form.type === "anthropic-compatible" ? "ANTHROPIC" : "OPENAI"} COMPATIBLE
          <span class="hint"><button type="button" class="k-btn ghost sm" onclick={() => (showForm = false)}>cancel</button></span>
        </h3>
        <div class="cfg-bar" class:on={addStaged}>
          <span class="st">{addStaged ? (form.base_url === "" ? "base url required" : "nothing written until ADD & TEST") : "nothing staged"}</span>
          <span class="sp"></span>
          <button type="button" class="k-btn pri" disabled={saving || form.base_url === ""} onclick={() => void save()}>
            {saving ? "SAVING…" : "ADD & TEST"}
          </button>
        </div>
        <div class="cfg-row">
          <div class="label">name
            <small>optional label shown in the model picker</small>
          </div>
          <div class="ctrl">
            <input type="text" bind:value={form.name} placeholder="e.g. DeepSeek, Ollama, Claude" />
          </div>
        </div>
        <div class="cfg-row">
          <div class="label">base url
            <small>required — the endpoint requests go to</small>
          </div>
          <div class="ctrl">
            <input type="url" bind:value={form.base_url} placeholder="http://localhost:11434/v1" />
          </div>
        </div>
        <div class="cfg-row">
          <div class="label">api key
            <small>optional for localhost</small>
          </div>
          <div class="ctrl">
            <input type="password" bind:value={form.api_key} placeholder="sk-… (optional for localhost)" />
          </div>
        </div>
        <div class="cfg-row">
          <div class="label">models
            <small>comma-separated; auto-fetched on save</small>
          </div>
          <div class="ctrl">
            <input type="text" bind:value={form.models} placeholder="deepseek-v4-flash-0731, local: qwen2.5-coder:7b" />
          </div>
        </div>
      </div>
      {/if}
    </div>
  </div>
