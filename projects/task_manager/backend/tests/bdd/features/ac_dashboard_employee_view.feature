# ac.dashboard.employee_view  (requirements: requirement.employee_dashboard; operations: operation.task.list_assigned)
Feature: View my dashboard succeeds

  Scenario: View my dashboard succeeds
    Given the user is signed in as Employee
    When List my assigned tasks
    Then The employee sees their assigned tasks
