/**
 * Sign in / create account — Contract 1.
 *
 * One component for both modes: the two forms differ only in which endpoint they call and what
 * the button says, and splitting them would mean maintaining the same validation twice.
 */

import { useState, type FormEvent } from 'react';
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from './AuthContext';
import { messageFor } from '../api/errors';
import { PASSWORD_MIN_LENGTH } from '../api/types';
import { Spinner, ErrorNotice } from '../ui/primitives';
import { ThemeToggle } from '../ui/ThemeToggle';

export type AuthMode = 'login' | 'signup';

/** Where the user was headed before they were bounced here. */
interface RedirectState {
  from?: string;
}

export function AuthPage({ mode }: { mode: AuthMode }) {
  const { status, login, signup } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);

  const from = (location.state as RedirectState | null)?.from;

  if (status === 'authenticated') {
    return <Navigate to={from ?? '/library'} replace />;
  }

  const isSignup = mode === 'signup';
  const passwordTooShort = password.length > 0 && password.length < PASSWORD_MIN_LENGTH;
  const canSubmit =
    email.trim().length > 0 && password.length >= PASSWORD_MIN_LENGTH && !submitting;

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setTouched(true);
    if (!canSubmit) return;

    setSubmitting(true);
    setError(null);
    try {
      const credentials = { email: email.trim(), password };
      if (isSignup) await signup(credentials);
      else await login(credentials);
      navigate(from ?? '/library', { replace: true });
    } catch (cause) {
      // Contract 9: `error` is one sentence, safe to render as-is.
      setError(messageFor(cause));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="auth">
      <aside className="auth__cover">
        <div className="auth__cover-inner">
          <p className="eyebrow auth__wordmark">Queryll</p>
          <h1 className="auth__statement">
            Ask your documents a question.
            <span className="auth__statement-accent"> Get the passage it came from.</span>
          </h1>
          <p className="auth__blurb">
            Upload what you have — papers, contracts, handbooks, notes. Queryll reads them, and
            every sentence of every answer carries a numbered citation you can open and check.
          </p>
          <ul className="auth__points">
            <li>
              <span className="auth__point-num">01</span>
              Answers are drawn only from your own documents.
            </li>
            <li>
              <span className="auth__point-num">02</span>
              Every claim cites the passage, with its page.
            </li>
            <li>
              <span className="auth__point-num">03</span>
              When the answer is not in there, it says so.
            </li>
          </ul>
        </div>
      </aside>

      <main className="auth__panel" id="main">
        <div className="auth__panel-top">
          <ThemeToggle />
        </div>

        <form className="auth__form" onSubmit={handleSubmit} noValidate>
          <header className="auth__form-head">
            <h2 className="auth__title">{isSignup ? 'Create your account' : 'Sign in'}</h2>
            <p className="auth__subtitle">
              {isSignup
                ? 'Your documents stay yours. Nobody else can search, cite or read them.'
                : 'Welcome back.'}
            </p>
          </header>

          {error ? <ErrorNotice>{error}</ErrorNotice> : null}

          <div className="field">
            <label className="field__label" htmlFor="auth-email">
              Email
            </label>
            <input
              id="auth-email"
              className="field__input"
              type="email"
              name="email"
              autoComplete="email"
              autoFocus
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>

          <div className="field">
            <label className="field__label" htmlFor="auth-password">
              Password
            </label>
            <input
              id="auth-password"
              className="field__input"
              type="password"
              name="password"
              autoComplete={isSignup ? 'new-password' : 'current-password'}
              required
              minLength={PASSWORD_MIN_LENGTH}
              aria-describedby="auth-password-hint"
              aria-invalid={touched && passwordTooShort ? true : undefined}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            {touched && passwordTooShort ? (
              <p className="field__error" id="auth-password-hint">
                Passwords must be at least {PASSWORD_MIN_LENGTH} characters.
              </p>
            ) : (
              <p className="field__hint" id="auth-password-hint">
                At least {PASSWORD_MIN_LENGTH} characters.
              </p>
            )}
          </div>

          <button className="btn btn--primary auth__submit" type="submit" disabled={!canSubmit}>
            {submitting ? <Spinner label="Signing in" /> : null}
            {isSignup ? 'Create account' : 'Sign in'}
          </button>

          <p className="auth__switch">
            {isSignup ? (
              <>
                Already have an account? <Link to="/login">Sign in</Link>
              </>
            ) : (
              <>
                New here? <Link to="/signup">Create an account</Link>
              </>
            )}
          </p>
        </form>
      </main>
    </div>
  );
}
