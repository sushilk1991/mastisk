import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  /** Changing this (a navigation) clears a caught error so the next view renders. */
  resetKey: string;
  /** Render nothing on failure — for optional chrome such as the ask drawer. */
  silent?: boolean;
  children: ReactNode;
}

interface State {
  error: Error | null;
  resetKey: string;
}

// Catches render errors and failed lazy-chunk loads (a stale tab after a
// deploy asks for a hashed chunk that no longer exists) so one broken view
// does not blank the whole shell.
export class ViewErrorBoundary extends Component<Props, State> {
  state: State = { error: null, resetKey: this.props.resetKey };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    if (props.resetKey === state.resetKey) return null;
    return { error: null, resetKey: props.resetKey };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('view failed to render', error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    if (this.props.silent) return null;
    return (
      <div className="view" role="alert">
        <div className="view-h">Something went wrong</div>
        <h1 className="view-title">Couldn't load this view.</h1>
        <p className="view-sub">
          The rest of Mastisk is still running. Reloading usually fixes it — a new version may
          have been deployed while this tab was open.
        </p>
        <button className="chip" type="button" onClick={() => window.location.reload()}>
          Reload
        </button>
      </div>
    );
  }
}
