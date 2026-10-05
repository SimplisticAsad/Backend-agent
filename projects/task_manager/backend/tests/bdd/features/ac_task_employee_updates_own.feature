# ac.task.employee_updates_own  (requirements: requirement.task_status; operations: operation.task.update_status)
Feature: Employees update only their own tasks (negative case)

  Scenario: Employees update only their own tasks (negative case)
    Given the user is signed in
    When an employee updates a task assigned to someone else
    Then Employees can only update tasks assigned to them (error TASK_NOT_ASSIGNED_TO_CALLER)
    And no data is changed
