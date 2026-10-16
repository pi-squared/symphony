defmodule SymphonyElixir.GitHubProjects.AdapterTest do
  use SymphonyElixir.TestSupport

  alias SymphonyElixir.GitHubProjects.Adapter
  alias SymphonyElixir.Orchestrator.State

  setup do
    directory = Path.join(System.tmp_dir!(), "projects-controller-#{System.unique_integer([:positive])}")
    File.mkdir_p!(directory)
    controller = Path.join(directory, "controller")
    on_exit(fn -> File.rm_rf!(directory) end)
    {:ok, controller: controller}
  end

  test "configuration, registry and provider credentials", %{controller: controller} do
    script(controller, %{"ok" => true, "result" => []})
    settings = settings(controller)
    assert Adapter.validate_config(settings) == :ok
    assert Adapter.validate_config(%{settings | provider: %{}}) == {:error, :invalid_github_projects_configuration}
    assert Adapter.validate_config(%{settings | active_states: []}) == {:error, :invalid_github_projects_configuration}
    assert {:error, :invalid_github_projects_configuration} = Adapter.validate_config(%{settings | terminal_states: []})
    assert Tracker.adapter_for_kind("github_projects") == {:ok, Adapter}
    assert "GH_TOKEN" in Adapter.secret_environment_names(settings)
    names = Enum.map(Adapter.agent_tool_specs(), & &1["name"])
    assert "github_api" in names
    assert "project_review" in names
    assert "project_validate" in names
    assert "project_evidence" in names
    refute "project_handoff" in names
  end

  test "real controller process maps normalized issues and timestamps", %{controller: controller} do
    script(controller, %{"ok" => true, "result" => [row("2026-10-15T10:00:00Z")]})
    configure(controller)
    assert {:ok, [issue]} = Adapter.fetch_issues_by_states(["Ready"])
    assert issue.id == "item-3"
    assert issue.native_ref["number"] == 3
    assert issue.dispatchable
    assert %DateTime{} = issue.created_at
    assert issue.updated_at == nil
    assert {:ok, [_issue]} = Adapter.fetch_issues_by_ids(["item-3"])
    script(controller, %{"ok" => true, "result" => [row("invalid")]})
    assert {:ok, [%Issue{created_at: nil}]} = Adapter.fetch_issues_by_ids(["item-3"])
    script(controller, %{"ok" => true, "result" => [row(nil)]})
    assert {:ok, [%Issue{created_at: nil}]} = Adapter.fetch_issues_by_ids(["item-3"])
  end

  test "provider errors and invalid subprocess responses remain failures", %{controller: controller} do
    configure(controller)
    script(controller, %{"ok" => false, "error" => "permission denied"}, 1)
    assert {:error, {:controller_error, "permission denied"}} = Adapter.fetch_issues_by_states(["Ready"])
    script(controller, %{"ok" => true, "result" => "invalid list"})
    assert {:error, :invalid_controller_issue_list} = Adapter.fetch_issues_by_ids([])
    File.write!(controller, "#!/bin/sh\nprintf invalid\nexit 2\n")
    assert {:error, {:invalid_controller_response, 2}} = Adapter.fetch_issues_by_ids([])
    File.rm!(controller)
    assert {:error, {:controller_unavailable, :enoent}} = Adapter.fetch_issues_by_ids([])
  end

  test "tools carry the bound issue and workspace and reject missing context", %{controller: controller} do
    settings = settings(controller)
    script(controller, %{"ok" => true, "result" => %{"status" => "in_review"}})
    opts = [tracker_settings: settings, issue: %Issue{id: "item-3"}, workspace: "/tmp/workspaces/GH-3"]
    response = Adapter.execute_agent_tool("project_claim", %{}, opts)
    assert response["success"]
    assert Jason.decode!(response["output"])["status"] == "in_review"
    assert [%{"type" => "inputText"}] = response["contentItems"]
    assert Adapter.execute_agent_tool("project_browser", %{"action" => "status"}, opts)["success"]
    script(controller, %{"ok" => false, "error" => "offline"}, 1)
    refute Adapter.execute_agent_tool("github_api", %{"method" => "GET", "path" => "/user"}, opts)["success"]
    script(controller, %{"ok" => true, "result" => %{"status" => "in_review"}})
    refute Adapter.execute_agent_tool("project_browser", %{}, tracker_settings: settings)["success"]
    refute Adapter.execute_agent_tool("project_claim", %{}, tracker_settings: settings)["success"]
    refute Adapter.execute_agent_tool("other_tool", %{}, opts)["success"]
    refute Adapter.execute_agent_tool("project_evidence", nil, opts)["success"]
    script(controller, %{"ok" => false, "error" => "head moved"}, 1)
    refute Adapter.execute_agent_tool("project_evidence", %{}, opts)["success"]
  end

  test "native API tool supports GraphQL through the bound host", %{controller: controller} do
    script(controller, %{"ok" => true, "result" => %{"status" => 200, "body" => %{"data" => %{"viewer" => %{"login" => "actor"}}}}})
    opts = [tracker_settings: settings(controller), issue: %Issue{id: "item-3"}, workspace: "/tmp/workspaces/GH-3"]
    result = Adapter.execute_agent_tool("github_api", %{"method" => "POST", "path" => "/graphql", "body" => %{"query" => "{viewer{login}}"}}, opts)
    assert result["success"]
    assert get_in(Jason.decode!(result["output"]), ["body", "data", "viewer", "login"]) == "actor"
  end

  test "browser tool is advertised only for a configured application worker", %{controller: controller} do
    script(controller, %{"ok" => true, "result" => []})
    configure(controller)
    refute Enum.any?(Adapter.agent_tool_specs(), &(&1["name"] == "project_browser"))
    path = Workflow.workflow_file_path()
    File.write!(path, String.replace(File.read!(path), "roles: {", "browser_enabled: true\n    roles: {"))
    assert :ok = SymphonyElixir.WorkflowStore.force_reload()
    assert Enum.any?(Adapter.agent_tool_specs(), &(&1["name"] == "project_browser"))
  end

  test "adapter order preserves execution order then priority then numeric issue" do
    ordered = %Issue{priority: 3, native_ref: %{"execution_order" => 1, "number" => 8}}
    high = %Issue{priority: 1, native_ref: %{"number" => 4}}
    low = %Issue{priority: 2, native_ref: %{"number" => 3}}
    assert Enum.sort_by([low, high, ordered], &Adapter.dispatch_order_key/1) == [ordered, high, low]
    assert Adapter.dispatch_order_key(nil) == {9_223_372_036_854_775_807, 5, 0}
  end

  test "native capacity admits a second candidate only when configured", %{controller: controller} do
    script(controller, %{"ok" => true, "result" => []})
    configure(controller)
    issue = %Issue{id: "second", identifier: "GH-4", title: "Second", state: "Ready", dispatchable: true}
    running = %{"first" => %{issue: %Issue{state: "Ready"}}}
    state = %State{running: running, max_concurrent_agents: 1}
    refute SymphonyElixir.Orchestrator.should_dispatch_issue_for_test(issue, state)
    assert SymphonyElixir.Orchestrator.should_dispatch_issue_for_test(issue, %{state | max_concurrent_agents: 2})
  end

  test "failed required local cleanup preserves the issue checkout", %{controller: controller} do
    script(controller, %{"ok" => true, "result" => []})
    configure(controller)
    root = Path.join(Path.dirname(controller), "workspaces")
    workspace = Path.join(root, "GH-3")
    File.mkdir_p!(workspace)
    File.write!(Path.join(workspace, "unfinished"), "preserve")
    workflow = File.read!(Workflow.workflow_file_path())
    workflow = String.replace(workflow, "---\nImplement the issue.", "workspace:\n  root: #{root}\nhooks:\n  before_remove: exit 1\n---\nImplement the issue.")
    File.write!(Workflow.workflow_file_path(), workflow)
    assert :ok = SymphonyElixir.WorkflowStore.force_reload()
    assert {:error, _, _} = SymphonyElixir.Workspace.remove(workspace)
    assert File.read!(Path.join(workspace, "unfinished")) == "preserve"
  end

  defp settings(controller) do
    %{
      provider: %{"repo" => "example/project", "controller" => controller, "roles" => %{"working" => "In progress"}},
      active_states: ["Ready", "In progress", "In review"],
      terminal_states: ["Done"]
    }
  end

  defp configure(controller) do
    path = Workflow.workflow_file_path()

    File.write!(path, """
    ---
    tracker:
      kind: github_projects
      provider:
        repo: example/project
        controller: #{controller}
        roles: {working: In progress}
      active_states: [Ready, In progress, In review]
      terminal_states: [Done]
    ---
    Implement the issue.
    """)

    SymphonyElixir.WorkflowStore.force_reload()
  end

  defp row(created) do
    %{
      "id" => "item-3",
      "identifier" => "GH-3",
      "title" => "Example",
      "description" => "Body",
      "native_ref" => %{"number" => 3},
      "state" => "Ready",
      "dispatchable" => true,
      "blocked_by" => [],
      "labels" => ["ready-for-agent"],
      "created_at" => created,
      "updated_at" => nil
    }
  end

  defp script(path, response, status \\ 0) do
    payload = response |> Jason.encode!() |> String.replace("'", "'\\''")
    File.write!(path, "#!/bin/sh\nprintf '%s\\n' '#{payload}'\nexit #{status}\n")
    File.chmod!(path, 0o700)
  end
end
