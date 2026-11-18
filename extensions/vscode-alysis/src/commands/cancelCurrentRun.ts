import * as vscode from "vscode";

import { ChatController } from "../chat/ChatController";

export function registerCancelCurrentRunCommand(
  context: vscode.ExtensionContext,
  chatController: ChatController
): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("alysis.cancelCurrentRun", async () => {
      if (chatController.hasActiveJob() || chatController.hasSession()) {
        await chatController.cancelCurrentRun();
        return;
      }
      await vscode.window.showInformationMessage("No active Alysis Code session is attached.");
    })
  );
}
