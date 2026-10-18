# ICA Lists for Home Assistant

Your ICA shopping lists as Home Assistant to-do lists, plus a **mirror** that copies any Home Assistant to-do list (Google Keep, the built-in shopping list, Bring!…) into an ICA list. Add "mjölk" by voice, on the dashboard or in Google Keep, and it's on the ICA list when you get to the store, sorted the way your store is laid out. Tick it off in the ICA app and it's ticked off at home too.

Unofficial and not affiliated with ICA.

## What it does

- **To-do lists:** each ICA list you pick becomes a to-do entity (e.g. `todo.ica_handla`). Add, rename, tick off and remove items from Home Assistant; changes made in the ICA app show up within a couple of minutes. Quantities set in the ICA app are shown and never wiped.
- **Mirror:** pick one Home Assistant to-do list and one ICA list.
  - Added in Home Assistant → added to ICA (or paired with an identical open item, so nothing is doubled).
  - Renamed, ticked off or removed in Home Assistant → the same in ICA.
  - Ticked off or removed in the ICA app → ticked off in Home Assistant.
  - Optional: items added in the ICA app are copied to the Home Assistant list too.
- **Safety:**
  - If many items vanish from the Home Assistant list at once, the mirror waits 3 minutes before removing them from ICA, in case the list just glitched.
  - Some lists (Google Keep among them) can fail quietly. The mirror reads the list back after each change, so it never undoes something in ICA because of that.

## Install

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/osterbergdesign/ica-lists-ha` as an **Integration**.
2. Install **ICA Lists**, then restart Home Assistant.
3. **Settings → Devices & services → Add integration → ICA Lists**.
4. Sign in with the **personnummer and password** you use on ica.se. If you only ever use BankID, set a password at ica.se first.
5. Pick the lists to show, then (optionally) the Home Assistant list to mirror into ICA.

Change lists, mirroring or how often ICA is checked under **Configure**. The action `ica_lists.sync_now` runs a check right away.

## How the sign-in works

It uses the same sign-in as the ICA app. Your password is used once to get a long-lived login token, which is saved in Home Assistant's storage and renewed automatically. With **Remember the password** ticked (the default), Home Assistant can sign in again by itself if ICA ever ends that login; otherwise it asks you. A refused password is never retried automatically, so the account can't get locked by repeated attempts.

## Requirements and limits

- **Swedish internet connection:** ICA only answers requests from Swedish IP addresses. A VPN exit outside Sweden will block it.
- **Password needed:** accounts that only have BankID can't be used until a password is set at ica.se.
- **Unofficial:** this uses ICA's private API, which ICA can change at any time. If the sign-in stops working, check for an update.
- **No conflict detection:** ICA has none. If two people edit the same item at the same moment, the last change wins.
- **No due dates or descriptions:** ICA has nothing to map them to, so Home Assistant's due dates and descriptions aren't synced.

## Troubleshooting

**Settings → Devices & services → ICA Lists → ⋮ → Download diagnostics** shows:

- your lists;
- the mirror's state;
- the last 40 events, such as sign-ins and items that were synced.

Diagnostics never include your personnummer, password or tokens.

## Credits

The login and list protocol was worked out by [kanylbullen/ica-mcp](https://github.com/kanylbullen/ica-mcp), [daha/ha-ica-shopping-barcode](https://github.com/daha/ha-ica-shopping-barcode) and [whiteout12/ha-ica-shopping-list](https://github.com/whiteout12/ha-ica-shopping-list) (all MIT). See NOTICE.

## Development

```bash
python3 tests/run.py            # client against a fake ICA server + sync rules
```

`tests/test_ha.py` runs the integration inside a real Home Assistant core when one is importable (it is skipped otherwise).
