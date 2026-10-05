# ac.dashboard.manager_progress  (requirements: requirement.project_progress; operations: operation.project.progress)
Feature: View project progress succeeds

  Scenario: View project progress succeeds
    Given the user is signed in as Manager
    When Get project progress
    Then The manager sees progress per project
