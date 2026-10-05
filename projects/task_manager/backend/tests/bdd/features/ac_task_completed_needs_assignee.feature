# ac.task.completed_needs_assignee  (requirements: requirement.task_status; operations: operation.task.update_status)
Feature: Completed tasks need an assignee (negative case)

  Scenario: Completed tasks need an assignee (negative case)
    Given the user is signed in
    When the user marks an unassigned task as completed
    Then A task cannot be completed without an assignee (error TASK_NO_ASSIGNEE)
    And no data is changed
