import * as vscode from "vscode";
import type { ChatController } from "../chat/ChatController";
import { activeWorkspaceFolder, workspaceScopeRequiredMessage } from "./activeWorkspace";
import { assertNoDirtyWorkspaceDocuments } from "./DirtyWorkspaceGuard";
import { createGitWorktree, emptyGitWorkspace, gitWorkspaceFailureReason, inspectGitWorkspace, type GitWorkspaceState } from "./GitWorktrees";
import { worktreeHandoffKey } from "./WorktreeHandoff";

/** Thin VS Code UI around independently testable, real Git operations. */
export class WorktreeController implements vscode.Disposable {
  private state = emptyGitWorkspace();
  private disposed = false;
  private refreshing = false;
  private busy = false;
  private readonly listeners: vscode.Disposable[];
  private readonly timer: ReturnType<typeof setInterval>;

  public constructor(private readonly chat: ChatController, private readonly changed: () => void,
    private readonly globalState: vscode.Memento) {
    this.listeners = [
      vscode.workspace.onDidSaveTextDocument(() => { void this.refresh(); }),
      vscode.workspace.onDidChangeWorkspaceFolders(() => { void this.refresh(); }),
      vscode.window.onDidChangeActiveTextEditor(() => { void this.refresh(); }),
      vscode.window.onDidChangeWindowState((state) => { if (state.focused) void this.refresh(); })
    ];
    this.timer = setInterval(() => { if (vscode.window.state.focused) void this.refresh(); }, 10_000);
    void this.refresh();
  }

  public dispose(): void {
    this.disposed = true;
    clearInterval(this.timer);
    this.listeners.forEach((listener) => listener.dispose());
  }

  private root(): string | undefined { return this.chat.activeSessionWorkspaceRoot() ?? activeWorkspaceFolder()?.uri.fsPath; }

  public snapshot(): GitWorkspaceState {
    const blocked = this.busy || this.chat.workspaceTransitionBlocked();
    return { ...this.state, busy: blocked, canMove: this.chat.canMoveToWorktree() && !blocked };
  }

  public async refresh(): Promise<void> {
    if (this.refreshing || this.disposed || this.busy) return;
    this.refreshing = true;
    const root = this.root();
    try {
      if (!vscode.workspace.isTrusted || !root) this.state = emptyGitWorkspace();
      else if (vscode.env.remoteName) this.state = emptyGitWorkspace("Worktree windows currently require a local repository.");
      else this.state = await inspectGitWorkspace(root);
    } catch (error) {
      this.state = emptyGitWorkspace(gitWorkspaceFailureReason(error));
    } finally {
      this.refreshing = false;
      if (!this.disposed && root === this.root()) this.changed();
      else if (!this.disposed) { this.state = emptyGitWorkspace(); void this.refresh(); }
    }
  }

  public async create(moveConversation = false): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    this.changed();
    try {
      if (!vscode.workspace.isTrusted) throw new Error("Trust this workspace before creating a worktree.");
      if (vscode.env.remoteName) throw new Error("Worktree windows currently require a local repository.");
      const root = this.root();
      if (!root) throw new Error(workspaceScopeRequiredMessage("creating a worktree"));
      if (moveConversation && !this.chat.canMoveToWorktree()) throw new Error("Start a conversation with the updated Alysis Code CLI before moving it.");
      await this.chat.withWorkspaceTransition(async () => {
        const repo = await inspectGitWorkspace(root);
        let includeChanges = moveConversation;
        if (!moveConversation) {
          const choice = await vscode.window.showQuickPick([
            { label: "Current commit", description: repo.branch, detail: "Start a fresh session with committed files.", copy: false },
            { label: "Include local changes", description: `${repo.files} changed files`, detail: "Copy staged edits, unstaged edits, and new files. Original files stay in place.", copy: true }
          ], { title: "New Worktree", placeHolder: "Choose the starting state", ignoreFocusOut: true });
          if (!choice) return;
          includeChanges = choice.copy;
        }
        const branch = await vscode.window.showInputBox({ title: moveConversation ? "Move to Worktree" : "New Worktree",
          prompt: moveConversation ? "Continue in a new window and branch. Saved local changes are copied; the original checkout is preserved." : "Name the new branch. The worktree opens in a separate VS Code window.",
          value: `alysis/session-${Date.now().toString(36)}`, ignoreFocusOut: true,
          validateInput: (value) => !value.trim() || value.length > 150 || value.startsWith("-") ? "Enter a branch name (up to 150 characters)." : undefined });
        if (branch === undefined) return;
        assertNoDirtyWorkspaceDocuments(vscode, root, "creating a worktree");
        if (this.root() !== root) throw new Error("The active workspace changed. Try again when it is idle.");
        await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification,
          title: moveConversation ? "Moving conversation to a worktree…" : "Creating a worktree…" }, async () => {
          const created = await createGitWorktree(root, branch.trim(), includeChanges);
          try {
            const reference = moveConversation ? await this.chat.prepareWorktreeHandoff(created.root) : undefined;
            await this.globalState.update(worktreeHandoffKey(created.root), { root: created.root, reference, createdAt: Date.now() });
            await vscode.commands.executeCommand("vscode.openFolder", vscode.Uri.file(created.root), { forceNewWindow: true });
          } catch (error) {
            throw new Error(`Worktree saved at ${created.root}. ${error instanceof Error ? error.message : String(error)}`);
          }
        });
      });
    } catch (error) {
      void vscode.window.showErrorMessage(error instanceof Error ? error.message : String(error));
    } finally { this.busy = false; this.changed(); void this.refresh(); }
  }

}
