// A trainer's explicit decision releases one ordering barrier without claiming
// the uncertain message was delivered or authorizing any replay.
export const isAbandonedText = entry => entry?.status === "unknown" &&
  entry.review?.decision === "abandon" && entry.review.actor === "trainer" &&
  entry.review.uuid === entry.uuid && entry.review.threadId === entry.threadId &&
  entry.review.bindingEpoch === entry.bindingEpoch;
export const isUnconfirmedText = entry => entry?.status === "dispatching" || entry?.status === "unknown" && !isAbandonedText(entry);
