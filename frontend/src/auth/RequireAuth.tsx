import { Navigate, useLocation } from 'react-router-dom';
import type { ReactNode } from 'react';
import { useAuth } from './AuthContext';
import { Spinner } from '../ui/primitives';

/**
 * Gate for every authenticated route.
 *
 * `restoring` renders a quiet placeholder rather than bouncing to login — bouncing first and
 * restoring second is the flicker that makes a refreshed tab feel like it logged you out.
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { status } = useAuth();
  const location = useLocation();

  if (status === 'restoring') {
    return (
      <div className="route-restoring">
        <Spinner label="Restoring your session" size={20} />
      </div>
    );
  }

  if (status === 'anonymous') {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  }

  return <>{children}</>;
}
