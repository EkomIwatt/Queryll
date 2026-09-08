import { Link, NavLink, Outlet } from 'react-router-dom';
import { useAuth } from './auth/AuthContext';
import { ThemeToggle } from './ui/ThemeToggle';
import { AskIcon, LibraryIcon, SignOutIcon } from './ui/icons';

/** The signed-in chrome: a thin masthead, two destinations, and as little else as possible. */
export function AppShell() {
  const { user, logout } = useAuth();

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>

      <header className="masthead">
        <Link className="masthead__brand" to="/library">
          <span className="masthead__mark" aria-hidden="true" />
          <span className="masthead__word">Queryll</span>
        </Link>

        <nav className="masthead__nav" aria-label="Main">
          <NavLink
            to="/library"
            className={({ isActive }) => `masthead__link${isActive ? ' masthead__link--on' : ''}`}
          >
            <LibraryIcon size={16} />
            Library
          </NavLink>
          <NavLink
            to="/ask"
            className={({ isActive }) => `masthead__link${isActive ? ' masthead__link--on' : ''}`}
          >
            <AskIcon size={16} />
            Ask
          </NavLink>
        </nav>

        <div className="masthead__end">
          <ThemeToggle />
          <span className="masthead__user" title={user?.email}>
            {user?.display_name}
          </span>
          <button
            type="button"
            className="btn btn--ghost btn--small"
            onClick={() => void logout()}
            aria-label="Sign out"
          >
            <SignOutIcon size={16} />
          </button>
        </div>
      </header>

      <div className="shell__body" id="main">
        <Outlet />
      </div>
    </div>
  );
}
