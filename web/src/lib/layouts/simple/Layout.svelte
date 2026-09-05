<script lang="ts">
  import { flip } from "svelte/animate";
  import { quintOut } from "svelte/easing";
  import { brain } from "../../store.svelte";
  import Chat from "../../components/Chat.svelte";
  import AgentState from "../../components/AgentState.svelte";
  import SessionsView from "../../components/SessionsView.svelte";
  import ConfigView from "../../components/ConfigView.svelte";
  import LogsView from "../../components/LogsView.svelte";
  import OnboardingView from "../../components/OnboardingView.svelte";
  import Toasts, { toast } from "../../components/Toasts.svelte";
  import AboutPanel from "../../components/AboutPanel.svelte";
  import Icon from "../../components/Icon.svelte";
  import { VIEW_ICONS } from "../../icons";
  import "./simple.css";
  import { createTabDrag } from "../../tab-drag.svelte";

  // Tab order is a user preference, so the strip is draggable (Alt+←/→ too).
  const drag = createTabDrag();

  /** Same view set, order and names as every other layout's menu; the glyph
   *  comes from the shared VIEW_ICONS map so the sidebar and the default
   *  layout's dock cannot label the same destination differently. */
  const nav = [
    ["workspace", "Workspace"],
    ["sessions", "Sessions"],
    ["config", "Config"],
    ["logs", "Logs"],
  ] as const;

  function titleFor(view: string): string {
    return nav.find((item) => item[0] === view)?.[1] ?? "Xu";
  }

  function tabTitle(id: string): string {
    return brain.sessions.find((item) => item.id === id)?.title || id;
  }

  function go(view: typeof nav[number][0]): void {
    brain.setView(view);
  }

  /** Both actions are RPCs that can fail (brain restarting, session deleted
   *  server-side). The old call sites fired `void brain.x()` and toasted
   *  "created"/nothing on the next line, so the toast claimed success before
   *  the call resolved and a failure left the UI silent. Await, then report. */
  async function newChat(): Promise<void> {
    try {
      await brain.newSession();
      toast(`New chat: ${brain.session?.title ?? "untitled"}`);
    } catch {
      toast("Could not start a new chat");
    }
  }

  async function openChat(id: string): Promise<void> {
    if (id === brain.session?.id && brain.view === "workspace") return;
    try {
      await brain.switchSession(id);
    } catch {
      toast(`Could not open ${tabTitle(id)}`);
    }
  }

  /** ESC → back to Home (or close the rail), ⌘/Ctrl+N → new chat. Ignored while typing. */
  function onKeydown(event: KeyboardEvent): void {
    const el = event.target as HTMLElement | null;
    const typing = !!el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable);
    if (event.key === "Escape" && !typing && brain.view !== "onboarding") {
      if (brain.view !== "workspace") {
        event.preventDefault();
        brain.setView("workspace");
        return;
      }
    }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "n") {
      event.preventDefault();
      void newChat();
    }
  }

  /** Approvals are rendered by Chat itself — jump to the chat that owns the
   *  oldest pending one instead of duplicating the card here. */
  function focusApproval(): void {
    const card = activeApprovals[0];
    if (!card) return;
    brain.setView("workspace");
    if (card.session_id && card.session_id !== brain.session?.id) void brain.switchSession(card.session_id);
  }
  const activeApprovals = $derived(brain.approvals.filter((item) => !item.resolved));
  /** Sub-agents currently streaming for this session. The activity modal is
   *  Chat's — this chip just asks Chat to open it (brain.requestSubRunId) and
   *  jumps to the workspace so Chat is mounted to consume the handshake. A
   *  fan-out runs several at once, so count them and open the first; Chat's
   *  tab strip reaches the rest. */
  const liveSubRuns = $derived(
    Object.values(brain.subRuns)
      .filter((r) => r.status === "running")
      .sort((a, b) => (a.started ?? 0) - (b.started ?? 0) || a.id.localeCompare(b.id)),
  );
  const liveSubRun = $derived(liveSubRuns[0] ?? null);
  // System status (RAM) — polled like the other layouts' status bars.
  let sysStatus = $state<{ rss_mb?: number | null }>({});
  const ramText = $derived(sysStatus.rss_mb == null ? "—" : `${Math.round(sysStatus.rss_mb)}MB`);
  $effect(() => {
    if (!brain.connected) return;
    let alive = true;
    const poll = async () => { try { const s = await brain.client.call<{ rss_mb?: number | null }>("app.status"); if (alive) sysStatus = s; } catch { /* keep last */ } };
    void poll();
    const t = setInterval(() => void poll(), 5000);
    return () => { alive = false; clearInterval(t); };
  });
  function openLiveSub(): void {
    const run = liveSubRun;
    if (!run) return;
    brain.setView("workspace");
    brain.requestSubRunId = run.id;
  }
  let aboutOpen = $state(false);

</script>

{#if brain.view === "onboarding"}
  <OnboardingView />
{:else}
  <div class="simple-shell">
    <aside class="simple-sidebar" aria-label="Main navigation">
      <button class="simple-brand" aria-label="About Xu" title="About Xu" onclick={() => (aboutOpen = true)}>
        {#if brain.avatar}
          <img class="simple-brand-avatar" src={brain.avatar} alt="Xu avatar" draggable="false" />
        {:else}
          X<span>u</span>
        {/if}
      </button>
      <nav class="simple-nav" aria-label="Xu sections">
        {#each nav as item}
          <button class:active={brain.view === item[0]} aria-current={brain.view === item[0] ? "page" : undefined} onclick={() => go(item[0])}>
            <span aria-hidden="true"><Icon name={VIEW_ICONS[item[0]]} size={18} /></span><span>{item[1]}</span>
          </button>
        {/each}
      </nav>
    </aside>

    <main class="simple-main">
      <header class="simple-topbar">
        <div><h1>{titleFor(brain.view)}</h1></div>
        <div class="simple-topbar-right">
          {#if liveSubRun}
            <button
              class="simple-sub-chip"
              onclick={openLiveSub}
              title={liveSubRuns.length > 1
                ? `${liveSubRuns.length} sub-agents are working — click to watch them live`
                : `Sub-agent ${liveSubRun.child} is working — click to watch live`}
            >
              <span class="simple-sub-dot" aria-hidden="true"></span>
              {liveSubRuns.length > 1
                ? `${liveSubRuns.length} sub-agents working…`
                : `${liveSubRun.child} working…`}
            </button>
          {/if}
          {#if activeApprovals.length}
            <button class="simple-approval-chip" onclick={focusApproval} title="Xu is waiting for a decision">
              <Icon name="triangle-alert" size={14} /> {activeApprovals.length} waiting for you
            </button>
          {/if}
          <span class="simple-meta" title="brain memory usage"><Icon name="memory-stick" size={13} /> {ramText}</span>
          <span class="simple-meta" title="active session id"><Icon name="hash" size={13} /> {brain.session?.id ?? "—"}</span>
          <div class="simple-links" role="status" aria-label="Channel status">
            <span class:offline={!brain.connected} title="Brain"><i></i><span class="lbl">brain</span></span>
          </div>
        </div>
      </header>

      {#if brain.view === "workspace"}

        <div class="simple-tabs" aria-label="Open chats" {...drag.strip()}>
          {#each brain.openSessionIds as id (id)}
            {@const name = tabTitle(id)}
            <div
              class="simple-tab"
              class:active={brain.session?.id === id}
              class:dragging={drag.dragging === id}
              animate:flip={{ duration: 220, easing: quintOut }}
              {...drag.tab(id)}
            >
              <button
                class="simple-tab-name"
                title={`${name} — drag to reorder (Alt+←/→)`}
                onclick={() => void openChat(id)}
                onkeydown={(e) => drag.onkeydown(e, id)}
              >
                {#if brain.isBusy(id)}<i class="simple-tab-busy" title="Running" aria-label="Running"></i>{/if}
                <span>{name}</span>
              </button>
              <button class="simple-tab-close" aria-label={`Close ${name}`} title="Close chat" onclick={() => { brain.closeSession(id); toast(`Closed ${name}`); }}><Icon name="x" size={14} /></button>
            </div>
          {/each}
          <button class="simple-tab-add" aria-label="Start a new chat" title="Start a new chat (⌘N)" onclick={() => void newChat()}><Icon name="plus" size={16} /></button>
        </div>

        <!-- Chat fills the viewport and scrolls itself, so the composer stays put
             instead of drifting down a page-length dashboard. -->
        <div class="simple-work">
          <section class="simple-card simple-chat-card">
            {#if brain.session}
              <Chat />
            {:else}
              <div class="simple-blank">
                <h2>Nothing open yet</h2>
                <p class="simple-empty">Start a chat and ask Xu for anything — a question, a task, a whole project.</p>
                <button class="simple-primary" onclick={() => void newChat()}><Icon name="message-square-plus" size={16} /> Start a new chat</button>
              </div>
            {/if}
          </section>

            <aside class="simple-card simple-rail" aria-label="Agent state">
              <div class="simple-rail-head">
                <h2>Agent state</h2>
              </div>
              <AgentState />
            </aside>
        </div>
      {:else}
        <div class="simple-view">
          {#if brain.view === "sessions"}
            <SessionsView />
          {:else if brain.view === "config"}
            <ConfigView embed />
          {:else if brain.view === "logs"}
            <LogsView />
          {/if}
        </div>
      {/if}
    </main>
  </div>
{/if}

<style>
  :global(body) { margin: 0; }
</style>

<svelte:window onkeydown={onKeydown} />
<Toasts />
  <AboutPanel open={aboutOpen} onclose={() => (aboutOpen = false)} />
