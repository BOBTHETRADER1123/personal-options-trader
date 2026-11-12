import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ArrowRight, Search } from 'lucide-react';
import { Modal } from './components';

const pages = [
  { id: 'overview', label: 'Overview', description: 'Your paper portfolio and desk status' },
  { id: 'positions', label: 'Positions & trades', description: 'Open strategies and saved trade history' },
  { id: 'chain', label: 'Option chain', description: 'Calls, puts and available contracts' },
  { id: 'builder', label: 'Strategy builder', description: 'Build and review a paper strategy' },
  { id: 'market', label: 'Market watch', description: 'Received quotes and source timestamps' },
  { id: 'daily', label: 'Daily Excel Downloads', description: 'Daily trading history and verified exports' },
  { id: 'exports', label: 'Trade reports', description: 'Individual trade summaries and archives' },
  { id: 'connections', label: 'Broker connections', description: 'Market data setup and connection status' },
  { id: 'settings', label: 'Data & settings', description: 'Paper assumptions, recording and account settings' },
];

export default function NavigationSearch({ onNavigate }: { onNavigate: (page: string) => void }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const resultsId = useId();
  const searchTerm = query.trim().toLocaleLowerCase();
  const results = pages.filter(page => `${page.label} ${page.description}`.toLocaleLowerCase().includes(searchTerm));

  const close = useCallback(() => {
    setOpen(false);
    setQuery('');
  }, []);

  useEffect(() => {
    function shortcut(event: KeyboardEvent) {
      if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        if (event.repeat) return;
        // Keep one dialog active so shared focus and scroll restoration stay predictable.
        if (!open && document.querySelector('[role="dialog"][aria-modal="true"]')) return;
        setQuery('');
        setOpen(previous => !previous);
      }
    }
    document.addEventListener('keydown', shortcut);
    return () => document.removeEventListener('keydown', shortcut);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    // Focus the search field after the shared dialog has mounted its focus handling.
    const frame = requestAnimationFrame(() => inputRef.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [open]);

  function choose(page: string) {
    close();
    onNavigate(page);
  }

  return <>
    <button
      type="button"
      className="desk-search-trigger"
      aria-label="Search workspace"
      aria-haspopup="dialog"
      aria-expanded={open}
      aria-keyshortcuts="Control+K Meta+K"
      onClick={() => { setQuery(''); setOpen(true); }}
    >
      <Search size={16} aria-hidden="true" />
      <span>Search</span>
      <kbd>Ctrl K</kbd>
    </button>
    {open && createPortal(<Modal title="Search workspace" subtitle="Go to a page in your trading desk." onClose={close}>
      <form className="desk-search-form" onSubmit={event => {
        event.preventDefault();
        if (results[0]) choose(results[0].id);
      }}>
        <div className="desk-search-input">
          <Search size={18} aria-hidden="true" />
          <input
            ref={inputRef}
            type="search"
            aria-label="Search workspace"
            aria-controls={resultsId}
            placeholder="Search pages…"
            value={query}
            onChange={event => setQuery(event.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
        </div>
        <div className="desk-search-results" id={resultsId}>
          {results.length ? <ul className="desk-search-list" aria-label="Workspace pages">
            {results.map(page => <li key={page.id}>
              <button type="button" className="desk-search-result" onClick={() => choose(page.id)}>
                <span><strong>{page.label}</strong><small>{page.description}</small></span>
                <ArrowRight size={16} aria-hidden="true" />
              </button>
            </li>)}
          </ul> : <p className="desk-search-empty" role="status">No matching pages. Try a page name such as positions or reports.</p>}
        </div>
        <div className="desk-search-footer"><span><kbd>Enter</kbd> Open first result</span><span><kbd>Esc</kbd> Close</span></div>
      </form>
    </Modal>, document.body)}
  </>;
}
