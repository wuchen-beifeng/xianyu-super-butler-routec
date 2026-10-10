import React from 'react';
import { createPortal } from 'react-dom';
import {
  AlertTriangle,
  CheckCircle2,
  Inbox,
  Loader2,
  LucideIcon,
  X,
} from 'lucide-react';

interface PageHeaderProps {
  title: string;
  description?: string;
  icon?: LucideIcon;
  badge?: React.ReactNode;
  actions?: React.ReactNode;
}

export const PageHeader: React.FC<PageHeaderProps> = ({
  title,
  description,
  icon: Icon,
  badge,
  actions,
}) => (
  <header className="page-header">
    <div className="page-header__identity">
      {Icon && (
        <span className="page-header__icon" aria-hidden="true">
          <Icon className="h-5 w-5" />
        </span>
      )}
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="page-title">{title}</h1>
          {badge}
        </div>
        {description && <p className="page-description">{description}</p>}
      </div>
    </div>
    {actions && <div className="page-actions">{actions}</div>}
  </header>
);

interface PageTabsProps<T extends string> {
  value: T;
  onChange: (value: T) => void;
  items: Array<{
    id: T;
    label: string;
    icon?: LucideIcon;
    count?: number;
  }>;
  ariaLabel?: string;
}

export const PageTabs = <T extends string>({
  value,
  onChange,
  items,
  ariaLabel = '页面分区',
}: PageTabsProps<T>) => (
  <div className="page-tabs" role="tablist" aria-label={ariaLabel}>
    {items.map((item) => {
      const Icon = item.icon;
      const active = value === item.id;
      return (
        <button
          key={item.id}
          type="button"
          role="tab"
          aria-selected={active}
          onClick={() => onChange(item.id)}
          className={`page-tab ${active ? 'page-tab--active' : ''}`}
        >
          {Icon && <Icon className="h-4 w-4 shrink-0" />}
          <span>{item.label}</span>
          {item.count !== undefined && <span className="page-tab__count">{item.count}</span>}
        </button>
      );
    })}
  </div>
);

interface SectionHeaderProps {
  title: string;
  description?: string;
  icon?: LucideIcon;
  actions?: React.ReactNode;
}

export const SectionHeader: React.FC<SectionHeaderProps> = ({
  title,
  description,
  icon: Icon,
  actions,
}) => (
  <div className="section-panel__header">
    <div className="flex min-w-0 items-start gap-3">
      {Icon && (
        <span className="section-header__icon" aria-hidden="true">
          <Icon className="h-4 w-4" />
        </span>
      )}
      <div className="min-w-0">
        <h2 className="section-title">{title}</h2>
        {description && <p className="section-description">{description}</p>}
      </div>
    </div>
    {actions && <div className="section-header__actions">{actions}</div>}
  </div>
);

interface NoticeBannerProps {
  type: 'success' | 'error' | 'warning' | 'info';
  message?: string;
  onClose?: () => void;
  children?: React.ReactNode;
}

export const NoticeBanner: React.FC<NoticeBannerProps> = ({
  type,
  message,
  onClose,
  children,
}) => {
  const Icon = type === 'success' ? CheckCircle2 : AlertTriangle;
  return (
    <div className={`notice-banner notice-banner--${type}`} role="status">
      <Icon className="h-4 w-4 shrink-0" />
      {/* 同时接受 message 与 children：只认 message 时，写成子元素的文案会被
          静默丢掉，页面上只剩一个感叹号图标，而 TS 不会报错（FC 隐式允许 children） */}
      <span className="min-w-0 flex-1">{message ?? children}</span>
      {onClose && (
        <button type="button" onClick={onClose} aria-label="关闭提示">
          <X className="h-4 w-4" />
        </button>
      )}
    </div>
  );
};

interface EmptyStateProps {
  title: string;
  description?: string;
  icon?: LucideIcon;
  action?: React.ReactNode;
  compact?: boolean;
}

export const EmptyState: React.FC<EmptyStateProps> = ({
  title,
  description,
  icon: Icon = Inbox,
  action,
  compact = false,
}) => (
  <div className={`empty-state ${compact ? 'empty-state--compact' : ''}`}>
    <span className="empty-state__icon" aria-hidden="true">
      <Icon className="h-6 w-6" />
    </span>
    <h3>{title}</h3>
    {description && <p>{description}</p>}
    {action && <div className="mt-4">{action}</div>}
  </div>
);

export const PageLoading: React.FC<{ label?: string }> = ({ label = '正在加载' }) => (
  <div className="page-loading" role="status">
    <Loader2 className="h-5 w-5 animate-spin" />
    <span>{label}</span>
  </div>
);

interface ConfirmDialogProps {
  /** false 时不渲染任何 DOM */
  open: boolean;
  title: string;
  /** 摘要区：调用方把「将要发生什么」逐项写清楚 */
  children?: React.ReactNode;
  confirmLabel: string;
  /** 危险主题：红色确认键 + 不可逆警示，且**不**自动聚焦确认键（防误触） */
  danger?: boolean;
  /** 要求输入完全匹配该文本才放行（如删除时输入商品 ID 后 4 位） */
  requireText?: string;
  loading?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * 通用二次确认弹窗（发布 / 删除这类不可逆写操作的统一门）。
 *
 * 两种加固模式可叠加：
 * - `danger`：红色主键 + 顶部不可逆警示；打开时焦点落在「取消」而不是确认键。
 * - `requireText`：必须一字不差地键入指定文本，确认键才从 disabled 变为可点。
 */
export const ConfirmDialog: React.FC<ConfirmDialogProps> = ({
  open,
  title,
  children,
  confirmLabel,
  danger = false,
  requireText,
  loading = false,
  onConfirm,
  onCancel,
}) => {
  const [typed, setTyped] = React.useState('');
  const inputRef = React.useRef<HTMLInputElement | null>(null);
  const confirmRef = React.useRef<HTMLButtonElement | null>(null);
  const cancelRef = React.useRef<HTMLButtonElement | null>(null);

  // 每次打开清空输入：上次残留的文本会让 requireText 模式被误放行
  React.useEffect(() => {
    if (open) setTyped('');
  }, [open]);

  // 聚焦策略：requireText → 输入框；danger 且无 requireText → 取消键（绝不自动聚焦确认键）
  React.useEffect(() => {
    if (!open) return;
    if (requireText !== undefined) {
      inputRef.current?.focus();
    } else if (danger) {
      cancelRef.current?.focus();
    } else {
      confirmRef.current?.focus();
    }
  }, [open, requireText, danger]);

  React.useEffect(() => {
    if (!open) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onCancel();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [open, onCancel]);

  if (!open) return null;

  const matched = requireText === undefined || typed === requireText;
  const confirmDisabled = loading || !matched;

  return createPortal(
    <div className="modal-overlay" role="presentation">
      <div
        className="modal-container"
        style={{ maxWidth: '32rem' }}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        data-testid="confirm-dialog"
        data-danger={danger ? 'true' : 'false'}
      >
        <div className="modal-header flex items-start gap-3">
          <span
            className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-full ${
              danger ? 'bg-red-100 text-red-600' : 'bg-gray-100 text-gray-600'
            }`}
            aria-hidden="true"
          >
            {danger ? <AlertTriangle className="h-5 w-5" /> : <CheckCircle2 className="h-5 w-5" />}
          </span>
          <div className="min-w-0">
            <h3 className={`text-lg font-bold ${danger ? 'text-red-600' : 'text-gray-900'}`}>
              {title}
            </h3>
            {danger && (
              <p className="mt-1 text-xs font-bold text-red-600">
                此操作不可撤销，请确认无误后再继续
              </p>
            )}
          </div>
        </div>

        <div className="modal-body space-y-4">
          {children}
          {requireText !== undefined && (
            <label className="block">
              <span className="text-sm font-semibold text-gray-700">
                请输入
                <code
                  className={`mx-1 rounded px-1.5 py-0.5 font-mono ${
                    danger ? 'bg-red-50 text-red-600' : 'bg-gray-100 text-gray-800'
                  }`}
                >
                  {requireText}
                </code>
                以确认
              </span>
              <input
                ref={inputRef}
                type="text"
                value={typed}
                autoComplete="off"
                spellCheck={false}
                placeholder={requireText}
                aria-label={`输入 ${requireText} 以确认`}
                data-testid="confirm-dialog-input"
                onChange={(event) => setTyped(event.target.value)}
                className="ios-input mt-1.5 w-full rounded-md px-3 py-2.5 font-mono text-sm"
              />
              {typed.length > 0 && !matched && (
                <span className="mt-1 block text-xs font-bold text-red-600">
                  输入不匹配，无法确认
                </span>
              )}
            </label>
          )}
        </div>

        <div className="modal-footer flex justify-end gap-2">
          <button
            ref={cancelRef}
            type="button"
            onClick={onCancel}
            disabled={loading}
            data-testid="confirm-dialog-cancel"
            className="ios-btn-secondary px-4 text-sm"
          >
            取消
          </button>
          <button
            ref={confirmRef}
            type="button"
            onClick={onConfirm}
            disabled={confirmDisabled}
            data-testid="confirm-dialog-confirm"
            className={`${
              danger ? 'ios-btn-danger' : 'ios-btn-primary'
            } flex items-center gap-2 px-4 text-sm disabled:cursor-not-allowed disabled:opacity-50`}
          >
            {loading ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : danger ? (
              <AlertTriangle className="h-4 w-4" />
            ) : null}
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
};
