- Memory verification now assembles each card's evidence per scope. Every bound workspace root
  is opened once as a held directory handle and checked against its pinned identity, and cited
  paths resolve under that handle one no-follow component at a time, so a root swapped, linked
  or recreated after the check is never read. Paths under no root, under two roots, under
  another scope's root or outside the evidence directories, bundles from an unlisted or shared
  archive project, and attested evidence ids are `evidence_unreachable`. Only verify receipts
  whose bytes match the evidence archive's recorded digest rank as authenticated, and only for
  the fields Brigade writes. Their command output is a separate medium item. Every item is cut
  on a UTF-8 boundary and scanned, and flagged items are dropped with their rule ids. A card
  without an authenticated receipt and a second independent item gets no model call.
