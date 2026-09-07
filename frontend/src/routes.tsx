import { Navigate, Route, Routes } from 'react-router-dom';
import { AppShell } from './AppShell';
import { AuthPage } from './auth/AuthPage';
import { RequireAuth } from './auth/RequireAuth';
import { ChatPage } from './chat/ChatPage';
import { DocumentPage } from './library/DocumentPage';
import { LibraryPage } from './library/LibraryPage';
import { EmptyState } from './ui/primitives';
import { Link } from 'react-router-dom';

/**
 * Routes are declared as elements rather than a data router so the whole tree can be mounted
 * inside a `MemoryRouter` in a test without a second routing configuration to keep in sync.
 */
export function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<AuthPage mode="login" />} />
      <Route path="/signup" element={<AuthPage mode="signup" />} />

      <Route
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route path="/" element={<Navigate to="/library" replace />} />
        <Route path="/library" element={<LibraryPage />} />
        <Route path="/library/:documentId" element={<DocumentPage />} />
        <Route path="/ask" element={<ChatPage />} />
        <Route path="/ask/:conversationId" element={<ChatPage />} />
      </Route>

      <Route
        path="*"
        element={
          <div className="notfound">
            <EmptyState
              eyebrow="404"
              title="There is nothing at this address."
              action={
                <Link className="btn btn--primary" to="/library">
                  Back to your library
                </Link>
              }
            />
          </div>
        }
      />
    </Routes>
  );
}
