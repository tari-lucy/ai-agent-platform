import '@mantine/core/styles.css';
import './styles.css';
import { MantineProvider } from '@mantine/core';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { Application } from './ui/Application.tsx';

const root = document.getElementById('root');
if (root === null) throw new Error('Panel root element is missing.');
createRoot(root).render(
  <StrictMode><MantineProvider theme={{ primaryColor: 'violet', fontFamily: 'Inter, system-ui, sans-serif', defaultRadius: 'md' }}>
    <Application />
  </MantineProvider></StrictMode>,
);
