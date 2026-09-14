// Deciding which hotlist items deserve an OS notification.
//
// Pure, so the rules are tested directly: the shell only delivers what this returns.

import type { Hotlist, HotlistItem } from "./api";

/** Alerts raised one by one before the rest are rolled into a single summary. */
export const MAX_INDIVIDUAL_ALERTS = 3;

/** Remembered ids per project; old ones fall off so storage stays bounded. */
const MAX_REMEMBERED = 500;

export interface CriticalAlert {
  itemId: string;
  title: string;
  body: string;
}

const storageKey = (projectId: string) => `osprey.alerted.${projectId}`;

/**
 * Item ids this device has already alerted on for a project, or null if it has
 * never seen the project. Browser storage can be unavailable; that reads as null.
 */
export function loadAlerted(projectId: string): Set<string> | null {
  try {
    const raw = localStorage.getItem(storageKey(projectId));
    return raw ? new Set(JSON.parse(raw) as string[]) : null;
  } catch {
    return null;
  }
}

export function saveAlerted(projectId: string, ids: Set<string>): void {
  try {
    localStorage.setItem(storageKey(projectId), JSON.stringify([...ids].slice(-MAX_REMEMBERED)));
  } catch {
    // Not fatal: at worst an item is alerted on again after a restart.
  }
}

function toAlert(item: HotlistItem): CriticalAlert {
  // A notice deadline is the thing that can waive a claim; say so up front.
  const prefix = item.notice_deadline ? "Notice deadline: " : "Act today: ";
  const body = [item.why, item.due ? `Due ${item.due}` : ""].filter(Boolean).join(" · ");
  return {
    itemId: item.item_id,
    title: `${prefix}${item.what}`.slice(0, 120),
    body: body.slice(0, 240),
  };
}

/**
 * The critical ("act today") items nobody has been alerted about yet.
 *
 * `alerted` is null the first time this device sees a project. Those items are
 * recorded without alerting: opening the app for the first time should not raise
 * one notification per item already on the list.
 *
 * Returns the alerts to raise and the updated set to remember.
 */
export function criticalAlerts(
  hotlist: Hotlist,
  alerted: Set<string> | null,
): { alerts: CriticalAlert[]; alerted: Set<string> } {
  const remembered = new Set(alerted ?? []);
  const fresh: HotlistItem[] = [];
  for (const item of hotlist.items) {
    if (item.bucket !== "act_today" || remembered.has(item.item_id)) continue;
    remembered.add(item.item_id);
    if (alerted !== null) fresh.push(item);
  }

  const alerts = fresh.slice(0, MAX_INDIVIDUAL_ALERTS).map(toAlert);
  const rest = fresh.length - MAX_INDIVIDUAL_ALERTS;
  if (rest > 0) {
    alerts.push({
      itemId: "summary",
      title: `${rest} more item${rest === 1 ? "" : "s"} need you today`,
      body: "Open Osprey to see the full hotlist.",
    });
  }
  return { alerts, alerted: remembered };
}
