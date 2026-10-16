defmodule SymphonyElixir.ParityProtocolTest do
  use SymphonyElixir.TestSupport
  alias SymphonyElixir.Config.Schema.Codex

  test "named permission profile omits legacy fields in thread and turn messages" do
    root = Path.join(System.tmp_dir!(), "parity-protocol-#{System.unique_integer([:positive])}")
    workspace = Path.join(root, "GH-3")
    File.mkdir_p!(workspace)
    on_exit(fn -> File.rm_rf!(root) end)
    script = Path.join(root, "codex")
    trace = Path.join(root, "trace")

    File.write!(script, """
    #!/bin/sh
    while IFS= read -r line; do
      printf '%s\\n' "$line" >> '#{trace}'
      case "$line" in
        *'"method":"initialize"'*) printf '%s\\n' '{"id":1,"result":{}}';;
        *'"method":"thread/start"'*) printf '%s\\n' '{"id":2,"result":{"thread":{"id":"profile-thread"}}}';;
        *'"method":"turn/start"'*)
          printf '%s\\n' '{"id":3,"result":{"turn":{"id":"profile-turn"}}}'
          printf '%s\\n' '{"method":"turn/completed"}'
          exit 0;;
      esac
    done
    """)

    File.chmod!(script, 0o700)

    File.write!(Workflow.workflow_file_path(), """
    ---
    tracker:
      kind: memory
      active_states: [Ready]
      terminal_states: [Done]
    workspace:
      root: #{root}
    codex:
      command: #{script}
      permissions: symphony
    ---
    Work.
    """)

    assert :ok = SymphonyElixir.WorkflowStore.force_reload()
    issue = %Issue{id: "3", identifier: "GH-3", title: "Profile", state: "Ready"}
    assert {:ok, _} = AppServer.run(workspace, "Run", issue)
    messages = trace |> File.read!() |> String.split("\n", trim: true) |> Enum.map(&Jason.decode!/1)
    thread = Enum.find(messages, &(&1["method"] == "thread/start"))["params"]
    turn = Enum.find(messages, &(&1["method"] == "turn/start"))["params"]
    assert thread["permissions"] == "symphony"
    refute Map.has_key?(thread, "sandbox")
    refute Map.has_key?(turn, "sandboxPolicy")
  end

  test "blank profile is invalid" do
    changeset = Codex.changeset(%Codex{}, %{"command" => "codex", "permissions" => " "})
    refute changeset.valid?
  end

  test "profile configuration rejects conflicting legacy overrides" do
    File.write!(Workflow.workflow_file_path(), """
    ---
    tracker:
      kind: memory
    codex:
      permissions: symphony
      thread_sandbox: workspace-write
    ---
    Work.
    """)

    assert {:error, _} = SymphonyElixir.WorkflowStore.force_reload()
  end
end
