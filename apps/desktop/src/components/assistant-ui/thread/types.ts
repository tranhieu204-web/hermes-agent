export interface RestoreMessageTarget {
  /** True when this is the first user turn on screen, so the rewind would
   *  delete the whole conversation. Drives the stronger confirmation copy and
   *  the dedicated gateway flag — the generic one is deliberately not enough. */
  wipesTranscript: boolean
  /** Gateway rewind identity for this turn; null when it isn't rewindable. */
  rewindId: string | null
  text: string
  /** Legacy positional fallback — only used against a gateway that mints no ids. */
  userOrdinal: number | null
}
