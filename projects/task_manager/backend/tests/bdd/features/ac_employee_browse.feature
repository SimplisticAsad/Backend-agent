# ac.employee.browse  (requirements: requirement.employee_directory; operations: operation.employee.list, operation.employee.read)
Feature: Browse Employees succeeds

  Scenario: Browse Employees succeeds
    Given the user is signed in as Manager
    And the record being worked on exists
    When List Employees
    And View Employee
    Then The Employee information is displayed
