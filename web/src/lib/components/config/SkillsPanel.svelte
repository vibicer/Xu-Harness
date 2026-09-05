<script lang="ts">
  import { brain } from "../../store.svelte";
  import Icon from "../Icon.svelte";
  let { activeModule = "skills" }: { activeModule?: string } = $props();
  let openSkills = $state<Record<string, boolean>>({});
  function toggleSkill(id: string): void {
    openSkills = { ...openSkills, [id]: !openSkills[id] };
  }
  const ambientSkills = $derived(brain.skills.filter((s) => s.ambient).length);
</script>

  <!-- ========== SKILLS ========== -->
  <div class="cfg-pane" class:active={activeModule === "skills"}>
    <div class="cfg-sec">
      <div class="cfg-sec-hd">
        <span class="t">SKILLS</span>
        <span class="d">{ambientSkills}/{brain.skills.length} on · enabled = always loaded, the rest load on demand</span>
      </div>
      {#if brain.skills.length === 0}
        <div class="k-empty">No skills installed.</div>
      {:else}
        {#each brain.skills as s (s.id)}
          {@const detail = !!(s.desc || s.keywords?.length)}
          <div class="cfg-row">
            <!-- The name folds when there is a description to fold, and is plain
                 text otherwise: a caret that expands nothing is a lie. Collapsed
                 shows the id, not the description — the id is one short line and
                 is what you pass to `skill_load`, so rows stay a uniform height
                 and the pane reads as a list. -->
            <svelte:element
              this={detail ? "button" : "div"}
              type={detail ? "button" : undefined}
              class="label"
              class:row-exp={detail}
              role={detail ? "button" : undefined}
              aria-expanded={detail ? !!openSkills[s.id] : undefined}
              onclick={detail ? () => toggleSkill(s.id) : undefined}
            >
              {#if detail}<span class="exp-caret"><Icon name={openSkills[s.id] ? "chevron-down" : "chevron-right"} size={12} /></span>{/if}
              {s.name}<small>{s.id}</small>
            </svelte:element>
            <div class="ctrl">
              <span class="chip-tag {s.ambient ? 'on' : 'off'}">{s.ambient ? "ON" : "OFF"}</span>
              <label class="toggle" title={s.ambient ? 'disable (stop always-loading)' : 'enable (always load)'}>
                <input type="checkbox" aria-label="always load skill {s.id}" checked={s.ambient} onchange={() => void brain.setSkill(s.id, !s.ambient)} />
                <span class="track"></span>
                <span class="thumb"></span>
              </label>
            </div>
          </div>
          {#if detail && openSkills[s.id]}
            <div class="tooldetail">
              {#if s.desc}<div class="td-row"><span class="td-desc">{s.desc}</span></div>{/if}
              {#if s.keywords?.length}
                <div class="td-row">
                  <span class="td-name">keywords</span>
                  <span class="td-desc">{s.keywords.join(" · ")}</span>
                </div>
              {/if}
            </div>
          {/if}
        {/each}
      {/if}
    </div>
  </div>
