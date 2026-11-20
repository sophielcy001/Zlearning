type BackupTarget = { name: string; model: string; address: string };
type Role = 'main' | 'review';
export type ModelBackups = Partial<Record<Role, BackupTarget>>;
export type BackupSelection = { allowMainBackup?: true; allowReviewBackup?: true };
export type ModelFailover = { role: Role; connection: BackupTarget; reason: string; message: string | null };

const names = { main: '主模型', review: '复核' } as const;

export function backupSelection(backups?: ModelBackups): BackupSelection {
  return { allowMainBackup: backups?.main ? true : undefined, allowReviewBackup: backups?.review ? true : undefined };
}

export function BackupPermission({ role, checked, disabled, onChange }: {
  role: '主模型' | '复核'; checked: boolean; disabled?: boolean; onChange: (value: boolean) => void;
}) {
  return <label className="backup-opt-in">
    <input type="checkbox" checked={checked} disabled={disabled} onChange={event => onChange(event.target.checked)} />
    <span>授权{role}备用连接</span>
  </label>;
}

export function BackupRecipient({ connection, label = '备用' }: { connection: BackupTarget; label?: string }) {
  return <div className="generation-destination">
    <strong>{label} · {connection.name} · {connection.model}</strong>
    <code>{connection.address}</code>
  </div>;
}

export function BackupOptions({ selection, onChange, mainOnly = false, disabled }: {
  selection: BackupSelection; onChange: (selection: BackupSelection) => void; mainOnly?: boolean; disabled?: boolean;
}) {
  return <div className="backup-options">
    <BackupPermission role="主模型" checked={Boolean(selection.allowMainBackup)} disabled={disabled}
      onChange={checked => onChange({ ...selection, allowMainBackup: checked ? true : undefined })} />
    {!mainOnly && <BackupPermission role="复核" checked={Boolean(selection.allowReviewBackup)} disabled={disabled}
      onChange={checked => onChange({ ...selection, allowReviewBackup: checked ? true : undefined })} />}
  </div>;
}

export function BackupDestinations({ backups }: { backups?: ModelBackups }) {
  return <>
    {backups?.main && <BackupRecipient connection={backups.main} label="主模型备用" />}
    {backups?.review && <BackupRecipient connection={backups.review} label="复核备用" />}
  </>;
}

export function FailoverRecord({ items }: { items?: ModelFailover[] }) {
  return items?.map(item => <p className="model-help" key={item.role}>
    {names[item.role]}切换到备用 · {item.connection.name} · {item.connection.model}
    {item.message && ` · ${item.message}`}
  </p>);
}
