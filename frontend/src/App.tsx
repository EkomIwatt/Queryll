import { useCallback, useMemo, useState } from 'react';
import { ApiProvider } from './api/ApiProvider';
import { AuthProvider } from './auth/AuthContext';
import { AppRoutes } from './routes';
import { createMockBackend } from './dev/mockBackend';

/**
 * Wiring, and one piece of real behaviour: when the API client's single refresh attempt fails,
 * the session is over. It reports that upward here, and the auth shell flips to `anonymous`,
 * which is what actually routes the reader to login (`RequireAuth`). The composer's draft lives
 * outside the router, so the typed question survives the trip.
 */
export function App() {
  const [sessionEndedAt, setSessionEndedAt] = useState(0);
  const handleSessionEnded = useCallback(() => setSessionEndedAt(Date.now()), []);

  // `VITE_MOCK_API=1 npm run dev` runs the whole UI against an in-memory backend, so the frontend
  // is demonstrable on its own while the real API lives in another worktree.
  const mockApi = useMemo(
    () => (import.meta.env.VITE_MOCK_API === '1' ? createMockBackend() : undefined),
    [],
  );

  return (
    <ApiProvider {...(mockApi ? { api: mockApi } : {})} onSessionEnded={handleSessionEnded}>
      <AuthProvider sessionEndedAt={sessionEndedAt}>
        <AppRoutes />
      </AuthProvider>
    </ApiProvider>
  );
}
