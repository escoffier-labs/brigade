- Hub push search now ranks and counts over push-eligible cards only. Corpus
  size, average card length and term frequency leave out private, pending,
  flagged and other writers' unreviewed cards, so adding or retiring a withheld
  card no longer moves the scores, order or `match_count` a connector sees.
- A push `card` read for an id with no card now does the same store lookup as
  one whose copies are all withheld, so the time of a 404 does not tell them
  apart.
