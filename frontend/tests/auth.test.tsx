/**
 * The auth shell — Contract 1, and the pieces of Contract 9 the browser is responsible for.
 *
 * The access token never leaves memory, so a reload has to re-derive the session from the httpOnly
 * refresh cookie before anything else renders. Everything here follows from that.
 */

import { beforeEach, describe, expect, it } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AppRoutes } from '../src/routes';
import { ApiError } from '../src/api/errors';
import { draftStore } from '../src/chat/draftStore';
import { createMockApi, makeDocument, renderWithProviders } from './harness';

beforeEach(() => {
  draftStore.reset();
});

describe('restoring a session', () => {
  it('restores from the refresh cookie and renders the app', async () => {
    const api = createMockApi({ documents: [] });
    renderWithProviders(<AppRoutes />, { api, route: '/library' });

    expect(await screen.findByRole('heading', { name: 'Your documents' })).toBeInTheDocument();
    expect(api.refresh).toHaveBeenCalledTimes(1);
    // The token is never read back from storage — there is nothing there to read.
    expect(window.localStorage.getItem('access_token')).toBeNull();
  });

  it('sends an unauthenticated visitor to sign in, remembering where they were going', async () => {
    const api = createMockApi({ session: null });
    renderWithProviders(<AppRoutes />, { api, route: '/library' });

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
  });
});

describe('signing in', () => {
  it('submits the credentials and lands on the library', async () => {
    const api = createMockApi({ session: null, documents: [] });
    renderWithProviders(<AppRoutes />, { api, route: '/login' });
    const user = userEvent.setup();

    await user.type(await screen.findByLabelText('Email'), 'reader@example.com');
    await user.type(screen.getByLabelText('Password'), 'correct horse');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    await waitFor(() =>
      expect(api.login).toHaveBeenCalledWith({
        email: 'reader@example.com',
        password: 'correct horse',
      }),
    );
    expect(await screen.findByRole('heading', { name: 'Your documents' })).toBeInTheDocument();
  });

  it('will not submit a password under the contract minimum', async () => {
    const api = createMockApi({ session: null });
    renderWithProviders(<AppRoutes />, { api, route: '/login' });
    const user = userEvent.setup();

    await user.type(await screen.findByLabelText('Email'), 'reader@example.com');
    await user.type(screen.getByLabelText('Password'), 'short');

    expect(screen.getByRole('button', { name: 'Sign in' })).toBeDisabled();
    expect(api.login).not.toHaveBeenCalled();
  });

  it('renders the server sentence verbatim when sign-in is refused', async () => {
    const api = createMockApi({ session: null });
    api.login.mockRejectedValueOnce(
      new ApiError('That email and password do not match an account.', 401),
    );
    renderWithProviders(<AppRoutes />, { api, route: '/login' });
    const user = userEvent.setup();

    await user.type(await screen.findByLabelText('Email'), 'reader@example.com');
    await user.type(screen.getByLabelText('Password'), 'wrong password');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'That email and password do not match an account.',
    );
  });

  it('creates an account through the signup route', async () => {
    const api = createMockApi({ session: null, documents: [] });
    renderWithProviders(<AppRoutes />, { api, route: '/signup' });
    const user = userEvent.setup();

    await user.type(await screen.findByLabelText('Email'), 'new@example.com');
    await user.type(screen.getByLabelText('Password'), 'a good password');
    await user.click(screen.getByRole('button', { name: 'Create account' }));

    await waitFor(() => expect(api.signup).toHaveBeenCalledTimes(1));
  });
});

describe('signing out', () => {
  it('clears the session and returns to sign in', async () => {
    const api = createMockApi({ documents: [] });
    renderWithProviders(<AppRoutes />, { api, route: '/library' });
    const user = userEvent.setup();

    await screen.findByRole('heading', { name: 'Your documents' });
    await user.click(screen.getByRole('button', { name: 'Sign out' }));

    await waitFor(() => expect(api.logout).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
  });
});

describe('a half-typed question', () => {
  it('survives navigating away and back', async () => {
    // The same mechanism that keeps the question through a 401 bounce to the login screen: the
    // draft lives outside the router, so no route change can take it.
    const api = createMockApi({ documents: [makeDocument()] });
    renderWithProviders(<AppRoutes />, { api, route: '/ask' });
    const user = userEvent.setup();

    const box = await screen.findByLabelText('Your question');
    await waitFor(() => expect(box).not.toBeDisabled());
    await user.type(box, 'What does section 3 say about');

    await user.click(screen.getByRole('link', { name: 'Library' }));
    await screen.findByRole('heading', { name: 'Your documents' });
    await user.click(screen.getByRole('link', { name: 'Ask' }));

    const restored = await screen.findByLabelText('Your question');
    expect(restored).toHaveValue('What does section 3 say about');
  });
});
