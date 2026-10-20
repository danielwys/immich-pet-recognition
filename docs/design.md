# Product design contract

The product helps someone find photos of their pets. Recognition is background work; naming
and correcting identities are ordinary library actions. Users do not manage training data,
validate a model, or finish a labeling queue before their albums become useful.

The appearance, navigation, and behavior must express that same purpose. Moving the old review
controls into new cards is not a redesign. The resting screen should look and act like a photo
library, with imagery taking most of the space and controls appearing in the relevant context.

## Screen hierarchy

**Library** is the default screen. Pet portraits, names, counts, and photo previews lead into
albums. Keep photos above operational information. The main navigation provides the library,
named pets, and Unidentified; settings and activity are secondary destinations.

Each **pet album** has a compact identity header and two tabs:

- **Photos:** current manual and automatic identities together in a dense contact sheet.
  Albums show complete Immich photo previews once per photo, with the relevant pet assignments
  retained underneath. Selecting incorrect photos reveals actions to move them to another pet or
  remove that name while preserving other pets in the photo.
  Browsing an album never asks the user to confirm a different pet.
- **More photos:** plausible additional matches, visibly separate from album members. The user
  selects matching photos and adds them in one action; Select shown supports larger batches.
  Suggestions are relevance-ranked, and nothing is preselected. This can cover
  several internal groups; the user need not repeatedly identify the same pet.

**Unidentified** shows visual groups in a compact contact sheet. Multiple groups can be selected
and named together from a fixed action bar. A group can be opened for closer inspection. Photos
may remain unidentified without becoming a persistent warning or mandatory completion target.

**Settings and activity** contain sorting preferences, per-pet pause controls, connection details,
progress, and retry. A small persistent activity indicator provides access while work runs.
Do not place a statistics wall or full sorting report above every photo view.

## Visual language

- Use a neutral white canvas, charcoal text, light gray separators, and a single crisp blue
  selection/action accent. Do not introduce a warm or cream theme.
- Keep the application header compact. Small pet portraits connect navigation to the albums;
  the album identity and its photos are the main visual hierarchy.
- Use a dense, responsive contact sheet with consistent image shapes and small gaps. Images
  should fill their tiles; repeated padded cards, explanatory paragraphs, and action buttons
  must not surround every photo.
- Reserve large type for the current album or library title. Counts, sync state, and secondary
  navigation stay quiet and readable. Avoid repeated eyebrow labels and dashboard headings.
- Use borders, modest corner radii, and whitespace to establish structure. Avoid oversized
  rounded panels, decorative shadows, and nested cards.
- A selection has one clear visual treatment and one contextual action bar. Keep its selected
  count and pet destinations visible together; do not scatter actions across tiles and toolbars.
- An image viewer gives the photo room, with a dark backdrop and compact navigation and naming
  controls. Closing it returns to the same position in the contact sheet.
- On narrow screens, adapt the photo columns and action bar without hiding the current task
  behind operational information. Preserve meaningful image sizes for identifying pets.

## Main user tasks

| Task | Route and action |
| --- | --- |
| Find photos of a pet | Library → pet → Photos. |
| Fix wrong identities | Select photos in that album → choose the correct pet or remove the name. |
| Add more plausible matches | Pet → More photos → select matching photos → Add to the pet. |
| Name several unidentified groups | Unidentified → select groups → choose a pet once. |
| Inspect a mixed group | Open it, inspect members, and exclude photos that do not belong. |
| Recover from a mistaken batch | Use the immediately available Undo action. |
| Check recognition or sync | Open the activity indicator; keep the library available. |

## Interaction contract

1. **An answer must not block the next answer.** Saving feedback, recalculating matches, and
   publishing Immich tags are separate operations. Keep the remaining gallery visible and
   usable while background work runs.
2. **Give immediate, local feedback.** A saved selection changes its visible state without a
   full-page reset. Preserve navigation, scroll, and unrelated selections. Surface failures
   beside the affected action and make recovery possible.
3. **Keep a browsing session stable.** Refreshing scores must not replace a contact sheet with
   a progress screen, discard selections, or make the user hunt for the next photo. Merge new
   results without interrupting the current task.
4. **Count results in photos.** Album counts describe current photos. Crop, detector, model,
   and gallery-revision details belong in technical activity details, if exposed at all.
   Suggestions must not be counted as album members before assignment. A saved name appears in its
   album immediately; publication to Immich is separately visible background sync.
5. **Every explicit name is feedback.** Do not expose reference, enrollment, validation,
   training-set, or “use as example” choices. Automatic matches must not train themselves.
6. **Make correction cheap.** Multi-selection, fixed destination actions, and Undo support
   ordinary mistakes. A wrong detector species must never stop a person naming their own pet.
7. **Treat current identity as the truth.** Do not mix old assignments into current albums or
   expose historical tagged/untagged states as additional review work. An undo checkpoint is
   recovery for an action, not a history browser.

## Why this structure

The earlier interface made internal similarity groups into repeated questions. Each answer
started recognition over the remaining library and removed the other questions until the pass
finished. Larger batches and different wording did not change that dependency or the user's
job of supervising every assignment.

Internal clusters are useful for arranging and selecting photos, but their similarity bounds,
species buckets, and size limits must not determine the number of answers a user owes. The
visible unit is a pet and its photos. Recognition does the routine sorting; people correct
mistakes and identify genuinely uncertain appearances when they choose.

This follows the collection and correction patterns documented by
[Apple Photos](https://support.apple.com/en-gb/guide/iphone/iph9c7ee918c/ios) and
[Google Photos](https://support.google.com/photos/answer/6128838?hl=en). These products inform
the workflow; they do not establish equivalent recognition accuracy for this service.

## Release acceptance

Verify the actual interaction with a realistically sized library, not only synthetic screens:

- Name several batches consecutively while recognition is active. The next batch must remain
  visible and actionable, without waiting for the whole library to be scored again.
- Browse a named pet and correct a selection. No unrelated confirmation question should appear.
- Add a candidate page after excluding mistakes; confirm that only the remaining candidates
  are assigned and that Undo recovers the batch.
- Refresh or restart after saved feedback. The visible identity and pending tag work must
  agree, and later edits must not be overwritten by Undo or background matching.
- Check desktop and narrow layouts. Photo inspection and the current selection's actions must
  remain accessible without navigating past operational reports.
