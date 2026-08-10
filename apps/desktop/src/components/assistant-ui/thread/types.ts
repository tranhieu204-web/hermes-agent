export interface RestoreMessageTarget {
  /** Gateway rewind identity for this turn; null when it isn't rewindable. */
  rewindId: string | null
  text: string
  /** Legacy positional fallback — only used against a gateway that mints no ids. */
  userOrdinal: number | null
}
