import type { ApprovalCard } from "../types";
import type { Ctor, StoreCoreBase } from "./core.svelte";

/**
 * Approval + clarification cards: the queue the overlay renders and the two
 * RPCs that answer one. A card's *lifecycle* is pushed by the brain — the
 * event router in store.svelte.ts adds a card on `turn.approval`, marks it
 * resolved on `turn.approval_resolved`, and calls `dismissSessionApprovals`
 * when a turn ends — so this mixin owns the array and the answers, not the
 * arrival path.
 *
 * Approval *modes* (`approval_mode` and its cycle) are config, not cards, and
 * stay with `config` / `saveConfig` in the composition root.
 */
export function ApprovalsMixin<T extends Ctor<StoreCoreBase>>(Base: T) {
  return class Approvals extends Base {
    approvals = $state<ApprovalCard[]>([]);

    /** A turn ending must not leave answered/stale cards behind: drop any
     * unresolved card for this session (and session-less ones, which have no
     * live owner). Resolved cards keep their brief "denied" flash then self-rm. */
    protected dismissSessionApprovals(sid: string): void {
      // Keep resolved cards (brief "denied" flash then self-rm) and cards of
      // other sessions; drop this session's unresolved ones and session-less
      // strays.
      this.approvals = this.approvals.filter(
        (a) => a.resolved !== null || (a.session_id && a.session_id !== sid),
      );
    }

    /** `remember` = the card's "always" button: stop prompting for this tool
     *  for the rest of the session (brain-side, RISKY only). */
    async resolveApproval(
      requestId: string,
      approved: boolean,
      remember = false,
    ): Promise<void> {
      await this.client.call("approval.resolve", {
        request_id: requestId,
        approved,
        remember,
      });
    }

    async replyAsk(requestId: string, answer: string): Promise<void> {
      await this.client.call("reply.resolve", { request_id: requestId, answer });
    }
  };
}
