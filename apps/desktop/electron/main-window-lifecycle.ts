type MainWindowLike = {
  isDestroyed: () => boolean
}

type MainWindowRevealTarget = {
  show: () => void
  showInactive: () => void
}

type FocusedWindowRevealTarget = MainWindowRevealTarget & {
  focus: () => void
}

type EnsureMainWindowOptions<T extends MainWindowLike> = {
  isReady: boolean
  createWindow: () => unknown
  focusWindow: (window: T) => unknown
  focusExisting?: boolean
}

export function ensureMainWindow<T extends MainWindowLike>(
  window: T | null | undefined,
  { isReady, createWindow, focusWindow, focusExisting = true }: EnsureMainWindowOptions<T>
) {
  if (!window || window.isDestroyed()) {
    // a closed electron window stays truthy, so replace it before invoking native methods.
    if (isReady) {
      createWindow()
    }

    return
  }

  if (focusExisting) {
    focusWindow(window)
  }
}

export function revealMainWindow(window: MainWindowRevealTarget, isPlaywrightTest: boolean) {
  if (isPlaywrightTest) {
    window.showInactive()

    return
  }

  window.show()
}

export function revealFocusedWindow(window: FocusedWindowRevealTarget, isPlaywrightTest: boolean) {
  if (isPlaywrightTest) {
    window.showInactive()

    return
  }

  window.show()
  window.focus()
}
