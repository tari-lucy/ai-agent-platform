import type { ManagedFile, SkillVersion, WorkplaceLibrary } from '../../shared/contracts.ts';

// Pure library logic: grouping and labels. No I/O. A skill's "uses" list describes its method;
// it is not a right, and a note or hypothesis is never presented as confirmed experience.
export const statusLabels: Record<SkillVersion['status'], string> = {
  experimental: 'Экспериментальный', verified: 'Проверен результатом', retired: 'Выведен из работы',
};
export const kindLabels: Record<ManagedFile['kind'], string> = {
  material: 'Материал', note: 'Заметка', hypothesis: 'Гипотеза', method: 'Метод', script: 'Скрипт',
  plan: 'План', draft: 'Черновик', result: 'Результат',
};
export const provenanceLabels: Record<ManagedFile['provenance'], string> = {
  read: 'прочитано', generated: 'собственный текст агента', assumption: 'предположение', verified: 'подтверждено',
};
export const areaLabels: Record<ManagedFile['area'], string> = {
  inbox: 'Входящие', knowledge: 'Знания', skills: 'Навыки', projects: 'Проекты', scratch: 'Черновики',
  exports: 'Выгрузки', reference: 'Справочники', guide: 'Руководства',
};

// Working skills first (verified, then experimental), newest change first; retired last.
export function skillOrder(rows: SkillVersion[]): SkillVersion[] {
  const rank = { verified: 0, experimental: 1, retired: 2 } as const;
  return [...rows].sort((a, b) => rank[a.status] - rank[b.status] ||
    Date.parse(b.updated_at) - Date.parse(a.updated_at) || a.skill_id.localeCompare(b.skill_id));
}
// Knowledge is what is not a skill's method; methods are shown with their skills.
export function knowledge(library: WorkplaceLibrary): { notes: ManagedFile[]; hypotheses: ManagedFile[]; other: ManagedFile[] } {
  const rows = [...library.files.filter((row) => row.area !== 'skills')]
    .sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at) || a.id.localeCompare(b.id));
  return {
    notes: rows.filter((row) => row.kind === 'note'),
    hypotheses: rows.filter((row) => row.kind === 'hypothesis'),
    other: rows.filter((row) => row.kind !== 'note' && row.kind !== 'hypothesis'),
  };
}
export function sourcesLabel(file: ManagedFile): string {
  const count = file.dependencies.length;
  return count === 0 ? 'без источников' : count === 1 ? '1 источник' : `${count} источн.`;
}
