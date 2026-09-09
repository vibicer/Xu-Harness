<script lang="ts">
  import { brain } from "../../store.svelte";
  import { notifySupported, type NotifySettings } from "../../notify";
  let { activeModule = "notifications" }: { activeModule?: string } = $props();
  const notifySettings = $derived(brain.notify);
  const notifyPerm = $derived(brain.notifyPerm);
  const setNotify = (patch: Partial<NotifySettings>): void => brain.setNotify(patch);
  const grantNotify = (): Promise<void> => brain.grantNotify();
  const testNotify = (): void => brain.testNotify();
</script>

  <!-- ========== NOTIFICATIONS ========== -->
  <div class="cfg-pane" class:active={activeModule === "notifications"}>
    <div class="cfg-sec">
      <div class="cfg-sec-hd">
        <span class="t">PERMISSION</span>
        <span class="d">OS-level — granted per browser, works while the Xu tab is open in the background</span>
      </div>
      {#if !notifySupported()}
        <div class="strow"><div class="t">This browser does not support desktop notifications.</div></div>
      {:else if notifyPerm === "granted"}
        <div class="cfg-row">
          <div class="label">notifications<small>browser permission granted</small></div>
          <div class="ctrl"><span class="key-badge ok">granted</span></div>
        </div>
      {:else if notifyPerm === "denied"}
        <div class="strow"><div class="t">Blocked — unblock Xu in the browser's site settings (lock icon → notifications), then reload.</div></div>
      {:else}
        <div class="cfg-row">
          <div class="label">notifications<small>the browser will ask once you click</small></div>
          <div class="ctrl"><button type="button" class="k-btn pri sm" onclick={() => void grantNotify()}>GRANT PERMISSION</button></div>
        </div>
      {/if}
    </div>

    <div class="cfg-sec">
      <div class="cfg-sec-hd">
        <span class="t">DESKTOP ALERTS</span>
        <span class="d">what pings you while you're away</span>
      </div>
      <div class="cfg-row">
        <div class="label">enable<small>master switch — nothing goes out when off</small></div>
        <div class="ctrl">
          <span class="state-badge">{notifySettings.enabled ? "ON" : "OFF"}</span>
          <label class="toggle">
            <input type="checkbox" aria-label="enable desktop notifications" checked={notifySettings.enabled} onchange={() => setNotify({ enabled: !notifySettings.enabled })} />
            <span class="track"></span>
            <span class="thumb"></span>
          </label>
        </div>
      </div>
      <div class="cfg-row">
        <div class="label">response done<small>a turn finished or failed in any session</small></div>
        <div class="ctrl">
          <label class="toggle">
            <input type="checkbox" aria-label="notify when a response is done" checked={notifySettings.turnDone} onchange={() => setNotify({ turnDone: !notifySettings.turnDone })} />
            <span class="track"></span>
            <span class="thumb"></span>
          </label>
        </div>
      </div>
      <div class="cfg-row">
        <div class="label">run failed<small>error alerts for broken turns</small></div>
        <div class="ctrl">
          <label class="toggle">
            <input type="checkbox" aria-label="notify when a run fails" checked={notifySettings.turnFailed} onchange={() => setNotify({ turnFailed: !notifySettings.turnFailed })} />
            <span class="track"></span>
            <span class="thumb"></span>
          </label>
        </div>
      </div>
      <div class="cfg-row">
        <div class="label">awaiting input<small>the agent is asking for approval or an answer</small></div>
        <div class="ctrl">
          <label class="toggle">
            <input type="checkbox" aria-label="notify when the agent is waiting for your input" checked={notifySettings.awaitInput} onchange={() => setNotify({ awaitInput: !notifySettings.awaitInput })} />
            <span class="track"></span>
            <span class="thumb"></span>
          </label>
        </div>
      </div>
      <div class="cfg-row">
        <div class="label">only when Xu unfocused<small>skip response alerts while you're typing in Xu</small></div>
        <div class="ctrl">
          <label class="toggle">
            <input type="checkbox" aria-label="only notify while Xu is unfocused" checked={notifySettings.onlyUnfocused} onchange={() => setNotify({ onlyUnfocused: !notifySettings.onlyUnfocused })} />
            <span class="track"></span>
            <span class="thumb"></span>
          </label>
        </div>
      </div>
      <div class="cfg-row">
        <div class="label">sound<small>browser notification sound</small></div>
        <div class="ctrl">
          <label class="toggle">
            <input type="checkbox" aria-label="play a notification sound" checked={notifySettings.sound} onchange={() => setNotify({ sound: !notifySettings.sound })} />
            <span class="track"></span>
            <span class="thumb"></span>
          </label>
        </div>
      </div>
      <div class="cfg-row">
        <div class="label">test alert<small>fires one now — clicking a notification focuses Xu</small></div>
        <div class="ctrl">
          <button type="button" class="k-btn" disabled={notifyPerm !== "granted"} onclick={testNotify}>Send test notification</button>
        </div>
      </div>
    </div>
  </div>
