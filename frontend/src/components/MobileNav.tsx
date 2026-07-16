import { Icon } from './icons';
import type { View } from '../types';

interface Props {
  currentView: View;
  menuOpen: boolean;
  onNavigate: (view: View) => void;
  onSearch: () => void;
  onChat: () => void;
  onCapture: () => void;
  onMenu: () => void;
}

export function MobileNav({ currentView, menuOpen, onNavigate, onSearch, onChat, onCapture, onMenu }: Props) {
  return (
    <nav className="mobile-nav" aria-label="Primary navigation">
      <button type="button" className={currentView === 'today' ? 'active' : ''} onClick={() => onNavigate('today')} aria-label="Today">
        <span className="mobile-nav-icon">◆</span><span>Today</span>
      </button>
      <button type="button" onClick={onSearch} aria-label="Search">
        <span className="mobile-nav-icon">{Icon.search}</span><span>Search</span>
      </button>
      <button type="button" className="mobile-nav-chat" onClick={onChat} aria-label="Chat">
        <span className="mobile-nav-icon">{Icon.ask}</span><span>Chat</span>
      </button>
      <button type="button" onClick={onCapture} aria-label="Capture">
        <span className="mobile-nav-icon mobile-nav-plus">+</span><span>Capture</span>
      </button>
      <button type="button" className={menuOpen ? 'active' : ''} onClick={onMenu} aria-label="Menu">
        <span className="mobile-nav-icon">{Icon.panel}</span><span>Menu</span>
      </button>
    </nav>
  );
}
