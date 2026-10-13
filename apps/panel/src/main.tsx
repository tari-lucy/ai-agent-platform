import '@mantine/core/styles.css';
import { Container, MantineProvider, Stack, Text, Title } from '@mantine/core';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

const root = document.getElementById('root');

if (root === null) {
  throw new Error('Panel root element is missing.');
}

createRoot(root).render(
  <StrictMode>
    <MantineProvider>
      <Container size="sm" py="xl">
        <Stack>
          <Title order={1}>AI Agent Platform</Title>
          <Text>Панель ещё не настроена. Агент не запущен.</Text>
        </Stack>
      </Container>
    </MantineProvider>
  </StrictMode>,
);
